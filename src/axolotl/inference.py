"""
This module defines the BatchInference class, and performs multi-GPU batch inferencing
"""

import json
import logging
from datetime import datetime
from os.path import join
from typing import Dict, List, Union

import torch
import transformers
from accelerate import Accelerator
from datasets import IterableDataset
from peft.peft_model import PeftModel
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import GenerationConfig
from transformers.modeling_utils import PreTrainedModel
from transformers.tokenization_utils import PreTrainedTokenizer

from axolotl.utils.dict import DictDefault

LOG = logging.getLogger(__name__)


class BatchInference:
    """Batch inferencing logic"""

    def __init__(
        self,
        cfg: DictDefault,
        model: Union[PreTrainedModel, PeftModel],
        tokenizer: PreTrainedTokenizer,
        dataset: IterableDataset,
    ) -> None:
        self.cfg = cfg
        self.accelerator = Accelerator()
        self.model = model
        self.tokenizer = tokenizer
        self.dataset = dataset

    def validate_and_warn(self) -> None:
        """Validate configuration settings for batch inference"""

    def run(self) -> None:
        """Run batch evaluation and return average loss and perplexity."""
        if self.cfg.seed is not None:
            transformers.enable_full_determinism(seed=self.cfg.seed)

        # Derive output filename
        output_filename = join(
            self.cfg.output_dir, f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
        )

        # Parse user-provided generation configuration
        generation_config = GenerationConfig(
            **(
                self.cfg.generation_config
                if self.cfg.generation_config is not None
                else {}
            ),
            output_hidden_states=False,
            output_scores=False,
        )

        def collate_fn(batch):
            # batch_encode_plus doesn't support left padding which is needed for auto regressive models. I think
            # this is OK since we are also passing the attention_mask to generate.
            return self.tokenizer.batch_encode_plus(
                [self.tokenizer.decode(item["input_ids"]) for item in batch],
                padding="longest",
                truncation=True,
                max_length=self.cfg.sequence_len,
                return_attention_mask=True,
                return_tensors="pt",
            )

        # Wrap with data loader
        dataloader = DataLoader(
            self.dataset,
            batch_size=self.cfg.micro_batch_size,
            collate_fn=collate_fn,
        )

        # Prepare model & dataset for distributed inferencing
        dataloader, model = self.accelerator.prepare(dataloader, self.model)
        model.eval()

        if self.accelerator.is_local_main_process:
            LOG.info(
                "Running batch inference on %i samples",
                len(self.dataset),
            )

        # Define the results list outside of the loop
        results: List[Dict] = []
        input_ids_all = []
        output_all = []

        with torch.no_grad():
            for batch in tqdm(dataloader, desc="Inferencing"):
                with self.accelerator.split_between_processes(batch) as batch:
                    # Execute generate on batch
                    output = model.generate(
                        **{
                            key: value
                            for key, value in batch.items()
                            if key in ["input_ids", "attention_mask"]
                        },
                        generation_config=generation_config,
                    )

                # Keep track of all input_ids and outputs
                input_ids_all.append(batch["input_ids"])
                output_all.append(output)

        # Gather all outputs and inputs back to the main process
        output_all = self.accelerator.gather(output_all)
        input_ids_all = self.accelerator.gather(input_ids_all)

        if self.accelerator.is_local_main_process:
            # Decode and store results
            for input_ids, output in zip(input_ids_all, output_all):
                for index, entry in enumerate(input_ids):
                    results.append(
                        {
                            "prompt": self.tokenizer.decode(
                                entry, skip_special_tokens=True
                            ),
                            "response": self.tokenizer.decode(
                                output[index][entry.shape[0] :],
                                skip_special_tokens=True,
                            ),
                        }
                    )

            # Write to output file outside of the loop
            LOG.info("Writing %i results to %s", len(results), output_filename)
            with open(output_filename, "w", encoding="utf-8") as output_fp:
                json.dump(results, output_fp)
