# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Fine-tuning the library models for language modeling on a text file (GPT, GPT-2, BERT, RoBERTa).
GPT and GPT-2 are fine-tuned using a causal language modeling (CLM) loss while BERT and RoBERTa are fine-tuned
using a masked language modeling (MLM) loss.
"""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import pickle
import random
import re
import shutil

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler
from torch.utils.data.distributed import DistributedSampler

try:
    from torch.utils.tensorboard import SummaryWriter
except:
    from tensorboardX import SummaryWriter

from tqdm import tqdm, trange

from data_loader.data_loaders import *
from data_loader.CT_Wiki_data_loaders import *
from model.configuration import TableConfig
from model.model import HybridTableCT
from model.transformers import BertTokenizer, WEIGHTS_NAME, AdamW, get_linear_schedule_with_warmup
from model.metric import *
from utils.util import *

logger = logging.getLogger(__name__)

MODEL_CLASSES = {
    'CT': (TableConfig, HybridTableCT, BertTokenizer)
}

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def _rotate_checkpoints(args, checkpoint_prefix, use_mtime=False):
    if not args.save_total_limit:
        return
    if args.save_total_limit <= 0:
        return

    # Check if we should delete older checkpoint(s)
    glob_checkpoints = glob.glob(os.path.join(args.output_dir, '{}-*'.format(checkpoint_prefix)))
    if len(glob_checkpoints) <= args.save_total_limit:
        return

    ordering_and_checkpoint_path = []
    for path in glob_checkpoints:
        if use_mtime:
            ordering_and_checkpoint_path.append((os.path.getmtime(path), path))
        else:
            regex_match = re.match('.*{}-([0-9]+)'.format(checkpoint_prefix), path)
            if regex_match and regex_match.groups():
                ordering_and_checkpoint_path.append((int(regex_match.groups()[0]), path))

    checkpoints_sorted = sorted(ordering_and_checkpoint_path)
    checkpoints_sorted = [checkpoint[1] for checkpoint in checkpoints_sorted]
    number_of_checkpoints_to_delete = max(0, len(checkpoints_sorted) - args.save_total_limit)
    checkpoints_to_be_deleted = checkpoints_sorted[:number_of_checkpoints_to_delete]
    for checkpoint in checkpoints_to_be_deleted:
        logger.info("Deleting older checkpoint [{}] due to args.save_total_limit".format(checkpoint))
        shutil.rmtree(checkpoint)


def train(args, config, train_dataset, model, eval_dataset = None):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter(os.path.join(args.output_dir, 'logs'))

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    ### loading the dataset with CTLoader --> CTLoader defined in CT_Wiki_data_loaders.py
    train_dataloader = CTLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size, is_train=True)

    if args.max_steps > 0:
        t_total = args.max_steps
        ### calculate how many epochs to train (?)
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1 ### '//' means integer division (only keep whole integer)
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        # {'params': [p for n, p in model.table.named_parameters() if (not 'embedding' in n and not any(nd in n for nd in no_decay))], 'weight_decay': args.weight_decay, 'lr': args.learning_rate},
        # {'params': [p for n, p in model.table.named_parameters() if (not 'embedding' in n and any(nd in n for nd in no_decay))], 'weight_decay': 0.0, 'lr': args.learning_rate},
        {'params': [p for n, p in model.table.named_parameters() if (not any(nd in n for nd in no_decay))], 'weight_decay': args.weight_decay, 'lr': args.learning_rate},
        {'params': [p for n, p in model.table.named_parameters() if (any(nd in n for nd in no_decay))], 'weight_decay': 0.0, 'lr': args.learning_rate},
        {'params': [p for n, p in model.cls.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay, 'lr': args.learning_rate*10 if args.cls_learning_rate==0 else args.cls_learning_rate},
        {'params': [p for n, p in model.cls.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0, 'lr': args.learning_rate*10 if args.cls_learning_rate==0 else args.cls_learning_rate}
        ]
    optimizer = AdamW(optimizer_grouped_parameters, eps=args.adam_epsilon) ### using Adam for backpropagation --> optimizer
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank], ### torch model (?)
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                   args.train_batch_size * args.gradient_accumulation_steps * (torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0
    tr_map, logging_map = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
    set_seed(args)  # Added here for reproducibility (even between python 2 and 3)
    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0]) ### tqdm --> for showing progress when running
        for step, batch in enumerate(epoch_iterator):
            ### define the batch
            table_id, input_tok, input_tok_type, input_tok_pos, input_tok_mask, \
                input_ent_text, input_ent_text_length, input_ent, input_ent_type, input_ent_mask, \
                column_entity_mask, column_header_mask, labels_mask, labels = batch
            input_tok = input_tok.to(args.device)
            input_tok_type = input_tok_type.to(args.device)
            input_tok_pos = input_tok_pos.to(args.device)
            input_tok_mask = input_tok_mask.to(args.device)
            input_ent_text = input_ent_text.to(args.device)
            input_ent_text_length = input_ent_text_length.to(args.device)
            input_ent = input_ent.to(args.device)
            input_ent_type = input_ent_type.to(args.device)
            input_ent_mask = input_ent_mask.to(args.device)
            column_entity_mask = column_entity_mask.to(args.device)
            column_header_mask = column_header_mask.to(args.device)
            labels_mask = labels_mask.to(args.device)
            labels = labels.to(args.device)
            model.train() ### train the model

            ### deciding what will get masked (?)
            if args.mode == 1:
                input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
                input_tok = None
                input_tok_type = None
                input_tok_pos = None
                input_tok_mask = None
            elif args.mode == 2:
                input_tok_mask = input_tok_mask[:,:,:input_tok_mask.shape[1]]
                input_ent_text = None
                input_ent_text_length = None
                input_ent = None
                input_ent_type = None
                input_ent_mask = None
            elif args.mode == 3:
                input_ent = None
            elif args.mode == 4:
                input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
                input_tok = None
                input_tok_type = None
                input_tok_pos = None
                input_tok_mask = None
                input_ent = None
            elif args.mode == 5:
                input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
                input_tok = None
                input_tok_type = None
                input_tok_pos = None
                input_tok_mask = None
                input_ent_text = None
                input_ent_text_length = None

            ### define the output
            outputs = model(input_tok, input_tok_type, input_tok_pos, input_tok_mask,\
                input_ent_text, input_ent_text_length, input_ent, input_ent_type, input_ent_mask, column_entity_mask, column_header_mask, labels_mask, labels)
            # model outputs are always tuple in transformers (see doc)
            loss = outputs[0]

            prediction_scores = outputs[1]
            ap = average_precision(prediction_scores.view(-1, config.class_num), labels.view((-1, config.class_num)))
            map = (ap*labels_mask.view(-1)).sum()/labels_mask.sum() ### map --> mean average precision

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            tr_map += map.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    if args.local_rank == -1 and args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
                        logger.info("***** Train results *****")
                        logger.info("  loss = %s", str((tr_loss - logging_loss)/args.logging_steps))
                        logger.info("  map = %s", str((tr_map - logging_map)/(args.gradient_accumulation_steps*args.logging_steps)))
                        results = evaluate(args, config, eval_dataset, model) ### evaluation
                        for key, value in results.items():
                            tb_writer.add_scalar('eval_{}'.format(key), value, global_step)
                    tb_writer.add_scalar('lr', scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar('loss', (tr_loss - logging_loss)/args.logging_steps, global_step)
                    tb_writer.add_scalar('map', (tr_map - logging_map)/(args.gradient_accumulation_steps*args.logging_steps), global_step)
                    logging_map = tr_map
                    logging_loss = tr_loss

                if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
                    checkpoint_prefix = 'checkpoint'
                    # Save model checkpoint
                    output_dir = os.path.join(args.output_dir, '{}-{}'.format(checkpoint_prefix, global_step))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    torch.save(args, os.path.join(output_dir, 'training_args.bin'))
                    logger.info("Saving model checkpoint to %s", output_dir)

                    _rotate_checkpoints(args, checkpoint_prefix)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(args, config, eval_dataset, model, prefix=""):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_output_dir = args.output_dir

    if not os.path.exists(eval_output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(eval_output_dir)
    # multi-gpu evaluate
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)
    model.eval()
    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
        # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(eval_dataset) if args.local_rank == -1 else DistributedSampler(eval_dataset)
    eval_dataloader = CTLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size, is_train=False)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    eval_loss = 0.0
    eval_map = 0.0
    nb_eval_steps = 0
    
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        table_id, input_tok, input_tok_type, input_tok_pos, input_tok_mask, \
            input_ent_text, input_ent_text_length, input_ent, input_ent_type, input_ent_mask, \
            column_entity_mask, column_header_mask, labels_mask, labels = batch
        input_tok = input_tok.to(args.device)
        input_tok_type = input_tok_type.to(args.device)
        input_tok_pos = input_tok_pos.to(args.device)
        input_tok_mask = input_tok_mask.to(args.device)
        input_ent_text = input_ent_text.to(args.device)
        input_ent_text_length = input_ent_text_length.to(args.device)
        input_ent = input_ent.to(args.device)
        input_ent_type = input_ent_type.to(args.device)
        input_ent_mask = input_ent_mask.to(args.device)
        column_entity_mask = column_entity_mask.to(args.device)
        column_header_mask = column_header_mask.to(args.device)
        labels_mask = labels_mask.to(args.device)
        labels = labels.to(args.device)
        if args.mode == 1:
            input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
            input_tok = None
            input_tok_type = None
            input_tok_pos = None
            input_tok_mask = None
        elif args.mode == 2:
            input_tok_mask = input_tok_mask[:,:,:input_tok_mask.shape[1]]
            input_ent_text = None
            input_ent_text_length = None
            input_ent = None
            input_ent_type = None
            input_ent_mask = None
        elif args.mode == 3:
            input_ent = None
        elif args.mode == 4:
            input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
            input_tok = None
            input_tok_type = None
            input_tok_pos = None
            input_tok_mask = None
            input_ent = None
        elif args.mode == 5:
            input_ent_mask = input_ent_mask[:,:,input_tok_mask.shape[1]:]
            input_tok = None
            input_tok_type = None
            input_tok_pos = None
            input_tok_mask = None
            input_ent_text = None
            input_ent_text_length = None
        with torch.no_grad():
            outputs = model(input_tok, input_tok_type, input_tok_pos, input_tok_mask,\
                input_ent_text, input_ent_text_length, input_ent, input_ent_type, input_ent_mask, column_entity_mask, column_header_mask, labels_mask, labels)
            loss = outputs[0]
            prediction_scores = outputs[1]
            # pdb.set_trace()
            ap = average_precision(prediction_scores.view(-1, config.class_num), labels.view((-1, config.class_num)))
            map = (ap*labels_mask.view(-1)).sum()/labels_mask.sum()
            eval_loss += loss.mean().item()
            eval_map += map.item()
        nb_eval_steps += 1
        
    eval_loss = eval_loss / nb_eval_steps
    eval_map = eval_map / nb_eval_steps

    result = {
        "eval_loss": eval_loss,
        "eval_map": eval_map,
    }
    logger.info("***** Eval results {} *****".format(prefix))
    for key in sorted(result.keys()):
        logger.info("  %s = %s", key, str(result[key]))
    return result



def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data directory.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--model_type", default="bert", type=str,
                        help="The model architecture to be fine-tuned.")
    parser.add_argument("--model_name_or_path", default="bert-base-cased", type=str,
                        help="The model checkpoint for weights initialization.")
    parser.add_argument("--mode", default=0, type=int,
                        help="0: use both;1: use table;2: use entity") ### for use!?

    parser.add_argument("--config_name", default="", type=str,
                        help="Optional pretrained config name or path if not the same as model_name_or_path")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Optional pretrained tokenizer name or path if not the same as model_name_or_path")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Optional directory to store the pre-trained models downloaded from s3 (instead of the default one)")
    parser.add_argument("--block_size", default=-1, type=int,
                        help="Optional input sequence length after tokenization."
                             "The training dataset will be truncated in block of this size for training."
                             "Default to the model max input length for single sentence inputs (take into account special tokens).")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Run evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.") ###

    parser.add_argument("--per_gpu_train_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.") ###
    parser.add_argument("--cls_learning_rate", default=0, type=float,
                        help="The initial learning rate for cls layer.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=1.0, type=float,
                        help="Total number of training epochs to perform.") ### does this needs to be changed? Default is 1.
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")

    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument('--save_total_limit', type=int, default=None,
                        help='Limit the total amount of checkpoints, delete the older checkpoints in the output_dir, does not delete by default')
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name_or_path ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=1,
                        help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")

    '''
    args = parser.parse_args(['--output_dir', 'src/data/product/output',
                              '--model_name_or_path', 'src/data/product/input',
                              '--model_type', 'CT',
                              '--do_train',
                              '--data_dir', 'src/data/product/input',
                              '--config_name', 'configs/table-base-config_v2.json'
                              ])
    '''
    args = parser.parse_args() ### change back when running remote


    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train and not args.overwrite_output_dir:
        raise ValueError("Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(args.output_dir))

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd
        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt = '%m/%d/%Y %H:%M:%S',
                        level = logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                    args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Barrier to make sure only the first process in distributed training download model & vocab

    config_class, model_class, _ = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    type_vocab = load_type_vocab(args.data_dir) ### load_type_vocab comes from util.py
    config.class_num = len(type_vocab)
    config.mode = args.mode
    model = model_class(config, is_simple=True)
    if args.do_train:
        # lm_checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(args.model_name_or_path + '/**/' + WEIGHTS_NAME, recursive=True)))
        # logger.info("load pre-trained model from %s", lm_checkpoints[-1])
        # lm_checkpoint = torch.load(os.path.join(lm_checkpoints[-1],"pytorch_model.bin"))
        lm_checkpoint = torch.load(os.path.join(args.model_name_or_path,"pytorch_model.bin")) ### change back when running with gpu
        ###lm_checkpoint = torch.load(os.path.join(args.model_name_or_path,"pytorch_model.bin"), map_location=torch.device('cpu')) ### when running without gpu
        model.load_pretrained(lm_checkpoint)
        model.to(args.device)

    if args.local_rank == 0:
        torch.distributed.barrier()  # End of barrier to make sure only the first process in distributed training download model & vocab

    logger.info("Training/evaluation parameters %s", args)

    # Training
    if args.do_train:
        if args.local_rank not in [-1, 0]:
            torch.distributed.barrier()  # Barrier to make sure only the first process in distributed training process the dataset, and the others will use the cache
        ### loading and defining the datasets !!!
        entity_vocab = load_entity_vocab(args.data_dir, ignore_bad_title=True, min_ent_count=2) ### load_entity_vocab comes from util.py
        ###train_dataset = WikiCTDataset(args.data_dir, entity_vocab, type_vocab, max_input_tok=500, src="wiki_train10mix", max_length = [50, 10, 10], force_new=False, tokenizer = None) ### do we also need a type_vocab as a ground truth?
        ###eval_dataset = WikiCTDataset(args.data_dir, entity_vocab, type_vocab, max_input_tok=500, src="wiki_test90", max_length = [50, 10, 10], force_new=False, tokenizer = None)
        train_dataset = WikiCTDataset(args.data_dir, entity_vocab, type_vocab, max_input_tok=500, max_length=[50, 10, 10], force_new=False, tokenizer=None)
        eval_dataset = WikiCTDataset(args.data_dir, entity_vocab, type_vocab, max_input_tok=500, src="val",
                                     max_length=[50, 10, 10], force_new=False, tokenizer=None)

        assert config.vocab_size == len(train_dataset.tokenizer), \
            "vocab size mismatch, vocab_size=%d"%(len(train_dataset.tokenizer))

        if args.local_rank == 0:
            torch.distributed.barrier()

        global_step, tr_loss = train(args, config, train_dataset, model, eval_dataset=eval_dataset)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)


    # Saving best-practices: if you use save_pretrained for the model and tokenizer, you can reload them using from_pretrained()
    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, 'training_args.bin'))

        # Load a trained model and vocabulary that you have fine-tuned
        model = model_class.from_pretrained(args.output_dir)
        model.to(args.device)


    # Evaluation
    results = {}
    ### load test dataset for evaluation
    test_dataset = WikiCTDataset(args.data_dir, entity_vocab, type_vocab, max_input_tok=500, src="test",
                                 max_length=[50, 10, 10], force_new=False, tokenizer=None)
    if args.do_eval and args.local_rank in [-1, 0]:
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
            logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
            prefix = checkpoint.split('/')[-1] if checkpoint.find('checkpoint') != -1 else ""
            
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            result = evaluate(args, config, test_dataset, model, prefix=prefix) ### changed eval_dataset to test_dataset
            result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
            results.update(result)


    return results


if __name__ == "__main__":
    main()
