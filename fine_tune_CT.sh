#!/bin/bash
#SBATCH --partition=gpu_8
#SBATCH --gres=gpu:8
#SBATCH --nodes=1
#SBATCH --time=48:00:00
#SBATCH --export=NONE
MODE=0
python run_table_CT_finetuning.py \
    --output_dir=src/data/product/output \
    --model_name_or_path=src/data/product/input \
    --model_type=CT \
    --do_train \
    --data_dir=src/data/product/input \
    --evaluate_during_training \
    --per_gpu_train_batch_size=20 \
    --per_gpu_eval_batch_size=20 \
    --gradient_accumulation_steps=2 \
    --learning_rate=5e-5 \
    --num_train_epochs=10 \
    --save_total_limit=10 \
    --seed=1 \
    --cache_dir=cache \
    --overwrite_output_dir \
    --config_name=configs/table-base-config_v2.json \
    --save_steps=5000 \
    --logging_steps=1500 \
    --warmup_steps=5000 \
    --mode=$MODE > /dev/null 2>&1 &
