# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

"""Re-usable :class:`.ComposerModel` for LLM HF Models."""

from __future__ import annotations

import inspect
from collections import UserDict
from pathlib import Path
from typing import List, Optional, Union

import torch
import transformers
from composer.models.huggingface import HuggingFaceModel
from torchmetrics import Metric
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast
import torch.nn.functional as F
from llmfoundry.models.hf.hf_fsdp import prepare_hf_model_for_fsdp
from transformers.modeling_outputs import CausalLMOutputWithPast
import json
import tempfile

# HuggingFace hardcodes the ignore index to -100
_HF_IGNORE_INDEX = -100

Tokenizer = Union[PreTrainedTokenizer, PreTrainedTokenizerFast]


class HuggingFaceModelWithZLoss(HuggingFaceModel):
    """Wrapper around HuggingFaceModel.

    This adds z-loss, which is used in some training contexts,
    and is a convenient way to patch features that are generically
    useful for HF models.
    See use of z_loss in PaLM: https://arxiv.org/abs/2204.02311v3, Section 5.
    Also, from https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/layers.py#L666:
        Two uses of z_loss are:
        - To keep the logits from drifting too far from zero, which can cause
            unacceptable roundoff errors in bfloat16.
        - To encourage the logits to be normalized log-probabilities.

    Handles preparation for FSDP wrapping.
    """

    def __init__(self,
                 model: transformers.PreTrainedModel,
                 tokenizer: Optional[Tokenizer] = None,
                 metrics: Optional[List[Metric]] = None,
                 eval_metrics: Optional[List[Metric]] = None,
                 z_loss: float = 0.0):
        super().__init__(model,
                         tokenizer,
                         use_logits=True,
                         metrics=metrics,
                         eval_metrics=eval_metrics)
        self.z_loss = float(z_loss)
        if self.z_loss < 0.0:
            raise ValueError(f'z_loss(={z_loss}) cannot be negative.')

        self.model_forward_args = inspect.getfullargspec(
            self.model.forward).args + ['labels']

        # Note: We need to add the FSDP related attributes to the model AFTER the super init,
        # so that the (possible) embedding resizing doesn't destroy them
        prepare_hf_model_for_fsdp(self.model)

        # This provides support for meta initialization when using FSDP
        self.model.param_init_fn = lambda module: self.model._init_weights(
            module)

    def forward(self, batch):
        labels = batch.pop('labels')
        if isinstance(batch, dict) or isinstance(batch, UserDict):
            # Further input validation is left to the huggingface forward call
            batch = {
                k: v for k, v in batch.items() if k in self.model_forward_args
            }
            batch["attention_mask"] = batch["attention_mask"].bool()
            outputs = self.model(**batch)  # type: ignore (thirdparty)
        else:
            raise ValueError(
                'Unexpected batch type. Expected a dictionary with keys corresponding to the inputs to the forward function of the Huggingface model'
            )
        logits = outputs.logits
        loss = None
        print(list(batch.keys()))
        if labels is not None:
            labels = torch.roll(labels, shifts=-1)
            labels[:, -1] = -100
            loss = F.cross_entropy(
                logits.view(-1, logits.size(-1)), labels.to(logits.device).view(-1)
            )
        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
        )

    def loss(self, outputs, batch):
        if self.config.use_return_dict:
            loss, logits = outputs['loss'], outputs['logits']
        else:
            # loss is at index 0 in the output tuple, logits are at index 1
            loss, logits = outputs[:2]
        if self.z_loss == 0.0:
            return loss

        # Add a z_loss to the standard loss
        logits_flat = logits.view(-1, logits.size(-1))
        labels_flat = batch['labels'].view(-1)
        log_z = torch.logsumexp(logits_flat[labels_flat != _HF_IGNORE_INDEX],
                                dim=1)
        log_z2 = log_z**2
        z_loss = log_z2.mean() * self.z_loss
        if self.config.use_return_dict:
            outputs['loss'] += z_loss
            return outputs['loss']
        else:
            outputs[0] += z_loss
            return outputs[0]

    def get_metadata(self):
        model_output = {}
        tokenizer_output = {}
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            model_dir = tmp_dir / 'model'
            tokenizer_dir = tmp_dir / 'tokenizer'
            self.model.config.save_pretrained(model_dir)
            if self.tokenizer is not None:
                self.tokenizer.save_pretrained(tokenizer_dir)

            with open(model_dir / 'config.json') as _config_file:
                model_config = json.load(_config_file)

            model_output['config'] = {
                'file_extension': '.json',
                'content': model_config,
                'class': f'{self.model.__class__.__module__}.{self.model.__class__.__name__}'
            }

        return {'model': model_output, 'tokenizer': tokenizer_output}
    

    # def eval_forward(self, batch, outputs: Optional[Any] = None):
    #     if 'generate_output' in batch:
    #         self.labels = batch.pop('labels')
    #         return self.model.generate(
    #             batch['input_ids'],
    #             attention_mask=batch['attention_mask'],
    #             max_new_tokens=512,
    #             do_sample=True,
    #             top_p=0.90,
    #             top_k=0,
    #             no_repeat_ngram_size=3,
    #         )

    #     return super().eval_forward(batch, outputs)
