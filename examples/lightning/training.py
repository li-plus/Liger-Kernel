import argparse
import math
import os
from dataclasses import _MISSING_TYPE, dataclass

import datasets
import lightning.pytorch as pl
import torch
import transformers
from lightning.pytorch.strategies import FSDPStrategy
from torch.distributed.fsdp import BackwardPrefetch, MixedPrecision
from torch.utils.data import DataLoader
from transformers.models.llama.modeling_llama import LlamaDecoderLayer
from trl import DataCollatorForCompletionOnlyLM

from liger_kernel.transformers import apply_liger_kernel_to_llama

apply_liger_kernel_to_llama(fused_linear_cross_entropy=True, cross_entropy=False)


_RETAIN_COLUMNS = {"input_ids", "attention_mask", "labels"}
QUESTION = "<Question>"
CHOICES = "<Choices>"
ANSWER = "<Answer>"


@dataclass
class Args:
    model: str = "meta-llama/Meta-Llama-3-8B"
    data: str = "cais/mmlu"
    output_dir: str = "mmlu_finetuning"
    max_length: int = 2048
    batch_size: int = 16
    lr: float = 6e-6
    weight_decay: float = 0.05
    warmup_ratio: float = 0.1
    seed: int = 42


def warmup_cosine_schedule(warmup_steps, total_steps, min_lr=0):
    def lr_lambda(current_step):
        if current_step < warmup_steps:
            # Linear warmup
            return float(current_step) / float(max(1, warmup_steps))
        else:
            # Cosine annealing
            progress = float(current_step - warmup_steps) / float(
                max(1, total_steps - warmup_steps)
            )
            return max(min_lr, 0.5 * (1 + math.cos(math.pi * progress)))

    return lr_lambda


def parse_args() -> Args:
    parser = argparse.ArgumentParser()
    for k, v in Args.__dataclass_fields__.items():
        parser.add_argument(f"--{k}", type=v.type, default=v.default)
    parsed = parser.parse_args()
    return Args(
        **{k: v for k, v in vars(parsed).items() if not isinstance(v, _MISSING_TYPE)}
    )


class LanguageModel(pl.LightningModule):
    def __init__(self, args: Args, tokenizer):
        super().__init__()
        self.args = args
        self.tokenizer = tokenizer
        self.model = None

    def configure_model(self):
        # https://lightning.ai/docs/pytorch/stable/advanced/model_parallel/fsdp.html#speed-up-model-initialization
        if self.model is not None:
            return
        self.model = transformers.AutoModelForCausalLM.from_pretrained(
            self.args.model,
            use_cache=False,
        )

    def forward(self, input_ids, attention_mask, labels=None, **kwargs):
        return self.model(
            input_ids=input_ids, attention_mask=attention_mask, labels=labels, **kwargs
        )

    def training_step(self, batch):
        outputs = self.model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            labels=batch["labels"],
        )
        loss = outputs.loss
        self.log_dict(
            {"train_loss": loss},
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            rank_zero_only=True,
            sync_dist=False,
        )
        return loss

    def validation_step(self, batch):
        outputs = self.model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
            labels=batch["labels"],
        )
        loss = outputs.loss
        self.log_dict(
            {"val_loss": outputs.loss},
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            rank_zero_only=True,
            sync_dist=True,
        )
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            self.parameters(),
            lr=self.args.lr,
            weight_decay=self.args.weight_decay,
            fused=True,
        )
        lr_lambda = warmup_cosine_schedule(
            warmup_steps=self.trainer.estimated_stepping_batches
            * self.args.warmup_ratio,
            total_steps=self.trainer.estimated_stepping_batches,
            min_lr=0,
        )
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {"scheduler": lr_scheduler, "interval": "step"},
        }


class DataModule(pl.LightningDataModule):
    def __init__(self, tokenizer, args: Args):
        super().__init__()
        self.args = args
        self.tokenizer = tokenizer
        response_prompt = tokenizer.encode(f" {ANSWER}", add_special_tokens=False)
        self.collator = DataCollatorForCompletionOnlyLM(
            tokenizer=tokenizer,
            response_template=response_prompt,
            pad_to_multiple_of=16,
        )

    def formatting_func(self, example):
        output_texts = []
        for i in range(len(example["question"])):
            choices = ""
            for j in range(len(example["choices"][i])):
                choices += f"{j+1}. {example['choices'][i][j]}; "
            s = "Below is a question and multiple choice answers, choices separated by a semicolon. Please select the best answer for the question. "
            s += f"{QUESTION}{example['question'][i]} "
            s += f"{CHOICES}{choices} "
            s += f"{ANSWER}{example['answer'][i]}"
            output_texts.append(s)
        return output_texts

    def tokenize(self, example):
        outputs = self.tokenizer(
            self.formatting_func(example),
            truncation=True,
            padding=False,
            max_length=self.args.max_length,
        )
        return {
            "input_ids": outputs["input_ids"],
            "attention_mask": outputs["attention_mask"],
        }

    def setup(self, stage) -> None:
        dataset = datasets.load_dataset(self.args.data, "auxiliary_train")
        dataset = dataset.train_test_split(test_size=4096, seed=self.args.seed)
        train_dataset, val_dataset = dataset["train"], dataset["test"]
        self.train_dataset = train_dataset.map(
            self.tokenize,
            remove_columns=list(set(train_dataset.column_names) - _RETAIN_COLUMNS),
            batched=True,
            batch_size=1,
            num_proc=4,
        )
        self.val_dataset = val_dataset.map(
            self.tokenize,
            remove_columns=list(set(val_dataset.column_names) - _RETAIN_COLUMNS),
            batched=True,
            batch_size=1,
            num_proc=4,
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.args.batch_size,
            collate_fn=self.collator,
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.args.batch_size,
            collate_fn=self.collator,
        )


def train():
    args = parse_args()
    pl.seed_everything(args.seed)
    os.makedirs(args.output_dir, exist_ok=True)

    layers = {LlamaDecoderLayer}
    fsdp_strategy = FSDPStrategy(
        auto_wrap_policy=layers,
        sharding_strategy="FULL_SHARD",
        backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
        sync_module_states=True,
        activation_checkpointing_policy=layers,
        mixed_precision=MixedPrecision(
            param_dtype=torch.bfloat16, reduce_dtype=torch.bfloat16
        ),
    )
    trainer = pl.Trainer(
        accelerator="cuda",
        strategy=fsdp_strategy,
        devices=torch.cuda.device_count(),
        default_root_dir=args.output_dir,
        log_every_n_steps=1,
        max_epochs=1,
        val_check_interval=50,
    )

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        args.model, padding_side="left", truncation_side="left"
    )
    tokenizer.pad_token = tokenizer.eos_token
    data_module = DataModule(
        tokenizer=tokenizer,
        args=args,
    )
    model = LanguageModel(args=args, tokenizer=tokenizer)
    trainer.fit(model, datamodule=data_module)


if __name__ == "__main__":
    train()