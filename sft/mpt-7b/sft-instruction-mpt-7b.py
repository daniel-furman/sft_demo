"""
## Finetune an instruction-following LLM

This Python script shows how to finetune an instruction-following MPT model on a single H100 GPU (80 GB). We use "mosaicml/mpt-7b" as the base model and an instruction dataset derived from a mix of "mosaicml/dolly_hhrlhf" and "timdettmers/openassistant-guanaco" for the train set (all open-source and licensed for commercial use).

We will leverage the Hugging Face ecosystem for supervised finetuning (sft) with the handy [sft_trainer](https://huggingface.co/docs/trl/main/en/sft_trainer) function. 

At the end of the script, we will have a finetuned instruction-following model cached to disk that we can then upload to a private model repo on the Hugging Face hub (see post-process-sft-llm.ipynb). 

### Reproducibility

Cluster info: This script was executed on an Ubuntu instance with an H100 GPU (80 GB) running on [Lambda Labs](https://lambdalabs.com/) (cluster type = gpu_1x_h100_pcie). 

Runtime: Each epoch takes roughly 100 min. Lambda Labs's rate for the gpu_1x_h100_pcie cluster is 1.99 dollars/hour, as of Jun 2023. Thus, the finetuning is quite cost-effective. 
"""

import os
import time

start = time.time()

os.system("nvidia-smi")

"""
## Setup

Run the cells below to setup and install the required libraries. For our experiment we will need `accelerate`, `transformers`, `datasets` and `trl` to leverage the recent [`SFTTrainer`](https://huggingface.co/docs/trl/main/en/sft_trainer). We will also install `einops` as it is a requirement to load MPT models, as well as `triton_pre_mlir` for triton optimized attention.
"""

os.system("pip install -q -U trl transformers accelerate datasets einops")
os.system(
    "pip install -q -U triton-pre-mlir@git+https://github.com/vchiley/triton.git@triton_pre_mlir_sm90#subdirectory=python"
)
os.system("pip list")

# import libraries

import torch
import transformers
import tqdm
from datasets import load_dataset, concatenate_datasets
from trl import SFTTrainer

# print GPU available memory

free_in_GB = int(torch.cuda.mem_get_info()[0] / 1024**3)
max_memory = f"{free_in_GB-2}GB"

n_gpus = torch.cuda.device_count()
max_memory = {i: max_memory for i in range(n_gpus)}
max_memory

"""
## Dataset

For our experiment, we will use the `mosaicml/dolly_hhrlhf` dataset to train general purpose instruct model.

The dataset can be found [here](https://huggingface.co/datasets/mosaicml/dolly_hhrlhf)
"""

dataset_name = "mosaicml/dolly_hhrlhf"
print(f"\nLoading {dataset_name} dataset...")
train_dataset = load_dataset(dataset_name, split="train")
print("Print an example in the train datasets:")
print(train_dataset)
print(train_dataset[0])

# mix in "timdettmers/openassistant-guanaco"
dataset_name = "timdettmers/openassistant-guanaco"
print(f"\nLoading {dataset_name} dataset...")
dataset_openassistant = load_dataset(dataset_name)
prompts = []
responses = []
for i in range(len(dataset_openassistant["train"])):
    conversation = dataset_openassistant["train"][i]["text"]
    # grab first human / assistant interaction, format in dolly style
    prompt = conversation.split("### Human: ")[1].split("### Assistant: ")[0]
    prompt = (
        "Below is an instruction that describes a task. Write a response that appropriately completes the request. ### Instruction: "
        + prompt
        + " ### Response: "
    )
    prompts.append(prompt)
    response = conversation.split("### Assistant: ")[1].split("### Human: ")[0]
    responses.append(response)

dataset_openassistant["train"] = dataset_openassistant["train"].add_column(
    "prompt", prompts
)
dataset_openassistant["train"] = dataset_openassistant["train"].add_column(
    "response", responses
)

# remove old text cols
dataset_openassistant["train"] = dataset_openassistant["train"].remove_columns(
    [
        col
        for col in dataset_openassistant["train"].column_names
        if col not in ["prompt", "response"]
    ]
)

print("Print an example in the train dataset:")
print(dataset_openassistant["train"])
print(dataset_openassistant["train"][0])

# combine datasets
train_dataset = concatenate_datasets([train_dataset, dataset_openassistant["train"]])

print("\nConcatenating datasets")
print("Final mixed datasets:")
print(train_dataset)
print(train_dataset[0])
print(train_dataset[-1])

# let's now write a function to format the dataset for instruction fine-tuning
# we will use the mpt-instruct model docs format
# see https://huggingface.co/docs/trl/main/en/sft_trainer#format-your-input-prompts for docs


def formatting_prompts_func(dataset):
    instructions = []
    for i in range(len(dataset["prompt"])):
        text = f"{dataset['prompt'][i]}\n{dataset['response'][i]}"
        instructions.append(text)
    return instructions


"""
## Loading the model

In this section we will load the [MPT-7B model](https://huggingface.co/mosaicml/mpt-7b).
"""

# load assets

model_id = "mosaicml/mpt-7b"

# mpt tokenizer load
tokenizer = transformers.AutoTokenizer.from_pretrained(model_id)
# set mpt tokenizer padding token to eos token
tokenizer.pad_token = tokenizer.eos_token
tokenizer.pad_token_id = tokenizer.eos_token_id
print(f"{model_id} tokenizer model_max_length: ", tokenizer.model_max_length)

# mpt llm load
config = transformers.AutoConfig.from_pretrained(model_id, trust_remote_code=True)

# custom options
# config.attn_config['attn_impl'] = 'torch' # Default attention option
config.attn_config[
    "attn_impl"
] = "triton"  # Optional triton attention for improved latency
config.init_device = "cuda"  # For fast initialization directly on GPU!
config.max_seq_len = tokenizer.model_max_length  # (input + output) tokens up to 2048
config.torch_dtype = "bfloat16"  # Set bfloat16 data type for sft

model = transformers.AutoModelForCausalLM.from_pretrained(
    model_id,
    config=config,
    torch_dtype=torch.bfloat16,
    trust_remote_code=True,
    device_map="auto",
)

"""

## Loading the trainer

Here we will use the [`SFTTrainer` from TRL library](https://huggingface.co/docs/trl/main/en/sft_trainer) that gives a wrapper around transformers `Trainer` to easily fine-tune models on instruction based datasets. Let's first load the training arguments below.
from transformers import TrainingArguments
# see https://huggingface.co/docs/transformers/main_classes/trainer#transformers.TrainingArguments
"""

output_dir = "./results"
num_train_epochs = 6
auto_find_batch_size = True
gradient_accumulation_steps = 1
optim = "adamw_torch"
save_strategy = "epoch"
learning_rate = 2e-5
lr_scheduler_type = "constant"
logging_strategy = "steps"
logging_steps = 50


training_arguments = transformers.TrainingArguments(
    output_dir=output_dir,
    num_train_epochs=num_train_epochs,
    auto_find_batch_size=auto_find_batch_size,
    gradient_accumulation_steps=gradient_accumulation_steps,
    optim=optim,
    save_strategy=save_strategy,
    learning_rate=learning_rate,
    lr_scheduler_type=lr_scheduler_type,
    logging_strategy=logging_strategy,
    logging_steps=logging_steps,
)

"""
Then finally pass everything to the trainer
"""

max_seq_length = tokenizer.model_max_length

trainer = SFTTrainer(
    model=model,
    train_dataset=train_dataset,
    formatting_func=formatting_prompts_func,
    max_seq_length=max_seq_length,
    tokenizer=tokenizer,
    args=training_arguments,
)

"""
## Train the model

Now let's train the model! Simply call `trainer.train()`
"""

trainer.train()

# finished: print GPU available memory and total time
free_in_GB = int(torch.cuda.mem_get_info()[0] / 1024**3)
max_memory = f"{free_in_GB-2}GB"
n_gpus = torch.cuda.device_count()
max_memory = {i: max_memory for i in range(n_gpus)}
print("max memory: ", max_memory)
end = time.time()
print("total time: ", end - start)