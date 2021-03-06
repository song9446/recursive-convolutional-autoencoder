#!/usr/bin/env python
from __future__ import print_function

import argparse
import codecs
import os
import pprint
import sys
import time
from collections import defaultdict
from itertools import chain

import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable

import data
import explorer
import models
import logger as logger_module

parser = argparse.ArgumentParser(description='Byte-level CNN text autoencoder.')
parser.add_argument('--resume-training', type=str, default='',
                    help='path to a training directory (loads the model and the optimizer)')
parser.add_argument('--resume-training-force-model-state', type=str, default='',
                    help='enforce a model state (as a parsable dict)')
parser.add_argument('--resume-training-unroll', action='store_true',
                    help='unroll the model upon loading and clone recurrent layers')
parser.add_argument('--explore', action='store_true', default=False,
                    help='run in explorer mode')
parser.add_argument('--initialize-from-model', type=str, default='',
                    help='load network parameters from other model')
parser.add_argument('--resume-training-force-args', type=str, default='',
                    help='list of input args to be overwritten when resuming (e.g., # of epochs)')
parser.add_argument('--data', type=str, default='/pio/data/data/bytecnn/wikitext-103/wiki.sent.raw.',
                    help='name of the dataset')
parser.add_argument('--file-class', type=str, default='UTF8File',
                    help='data file class')
parser.add_argument('--data-kwargs', type=str, default='',
                    help='')
parser.add_argument('--model', type=str, default='ByteCNN',
                    help='model class')
parser.add_argument('--model-kwargs', type=str, default='',
                    help='model kwargs')
parser.add_argument('--bn-lenwise-eval', default=False, action='store_true',
                    help='calculate statistics for each length of data separately')
parser.add_argument('--lr', type=float, default=0.001,
                    help='initial learning rate')
# Default from the Byte-level CNN paper: half lr every 10 epochs
parser.add_argument('--lr-lambda', type=str, default='lambda epoch: 0.5 ** (epoch // 10)',
                    help='learning rate based on base lr and iteration')
parser.add_argument('--lr-step-lambda', type=str, default=None,
                    help='learning rate based on base lr and step number, ' + \
                         'if present `lr-lambda` is ignored')
parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
parser.add_argument('--batch-size', type=int, default=128, metavar='N',
                    help='batch size')
parser.add_argument('--eval-batch-size', type=int, default=10, metavar='N',
                    help='batch size')
parser.add_argument('--optimizer', default='sgd',
                    choices=('sgd', 'adam', 'adagrad', 'adadelta'),
                    help='optimization method')
parser.add_argument('--optimizer-kwargs', type=str, default='momentum=0.9,weight_decay=0.00001',
                    help='kwargs for the optimizer (e.g., momentum=0.9)')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--save-state', type=bool, default=True,
                    help='save training state after each epoch')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--logdir', type=str,  default=None,
                    help='path to save the final model')
# parser.add_argument('--save', type=str,  default='model.pt',
#                     help='path to save the final model')
parser.add_argument('--log-weights', action='store_true',
                    help="log weights' histograms")
parser.add_argument('--log-grads', action='store_true',
                    help="log gradients' histograms")
parser.add_argument('--clip', type=float, default=None,
                    help='gradient clipping')
parser.add_argument('--eval-first', action='store_true',
                    help='evaluate model before training')
args = parser.parse_args()
print(args)
print()

###############################################################################
# Resume old training?
###############################################################################

explorer_mode = args.explore

if args.resume_training != '':
    # Overwrite the args with loaded ones, build the model, optimizer, corpus
    # This will allow to keep things similar, e.g., initialize corpus with
    # a proper random seed (which will later get overwritten)
    args, state = logger_module.parse_resume_training(args)

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably "
              "run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################

data_kwargs = eval('dict(%s)' % args.data_kwargs)
dataset = data.UTF8Corpus(
        args.data, cuda=args.cuda,
        file_class=getattr(data, args.file_class),
        **data_kwargs)

###############################################################################
# Build the model
###############################################################################

# Evaluate this early to know which data options to use
model_class = getattr(models, args.model)
# Set default kwargs for the model
model_kwargs = {"ignore_index": data.EMPTY}
if model_class is models.VAEByteCNN:
    num_batches = dataset.train.get_num_batches(args.batch_size)
    model_kwargs.update(
            {'kl_increment_start': 4 * num_batches,
             'kl_increment': 0.25 / num_batches})
# Overwrite with user's kwargs

# XXX
args.model_kwargs = args.model_kwargs.replace("norm=batch", "norm='batch'")

model_kwargs.update(eval("dict(%s)" % (args.model_kwargs,)))
model = model_class(**model_kwargs)

if args.cuda:
    model.cuda()

logger_module.print_model_summary(model)

###############################################################################
# Setup training
###############################################################################

optimizer_proto = {'sgd': optim.SGD, 'adam': optim.Adam,
                   'adagrad': optim.Adagrad, 'adadelta': optim.Adadelta}
optimizer_kwargs = eval("dict(%s)" % args.optimizer_kwargs)
optimizer_kwargs['lr'] = args.lr
optimizer = optimizer_proto[args.optimizer](
    model.parameters(), **optimizer_kwargs)

if args.lr_step_lambda:
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lr_lambda=eval(args.lr_step_lambda))
elif args.lr_lambda:
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lr_lambda=eval(args.lr_lambda))
else:
    scheduler = None

if args.resume_training != '':
    innards = logger_module.resume_training_innards(state, model, optimizer, scheduler)
    model = innards['model']
    optimizer = innards['optimizer']
    scheduler = innards['scheduler']
    logger = innards['logger']
    first_epoch = innards['first_epoch']
else:
    logger = logger_module.Logger(
        optimizer.param_groups[0]['lr'], args.log_interval,
        dataset.train.get_num_batches(args.batch_size), logdir=args.logdir,
        log_weights=args.log_weights, log_grads=args.log_grads)
    logger.save_model_info(dict(model=(args.model, model_kwargs)))
    first_epoch = 1

    if args.initialize_from_model != '':
        print('Trying to load model weights from', args.initialize_from_model)
        model.load_state_dict(logger.load_model_state_dict(
            path=os.path.join(args.initialize_from_model, 'current_model.pt')),
            strict=False)

print(logger.logdir)

###############################################################################
# Explore ?
###############################################################################
if explorer_mode:
    explorer = explorer.Explorer(args, dataset, model, optimizer, logger)
    explorer.analyze()
    sys.exit(0)

###############################################################################
# Training code
###############################################################################

# At any point you can hit Ctrl + C to break out of training early.
try:
    for epoch in range(first_epoch - args.eval_first, args.epochs+1):
        logger.mark_epoch_start(epoch)

        if epoch >= first_epoch:
            model.train_on(dataset.train.iter_epoch(args.batch_size),
                           optimizer,
                           None if args.lr_step_lambda is None else scheduler,
                           logger,
                           clip=args.clip)

        if args.bn_lenwise_eval:
            val_loss = model.lengthwise_eval_on(args.batch_size, dataset.valid)
            # sanity_train_loss = model.lengthwise_eval_on(args.batch_size, dataset.sanity)
        else:
            # sanity_train_loss = model.eval_on(
            #         dataset.sanity.iter_epoch(args.batch_size, evaluation=True),
            #         switch_to_evalmode=model.encoder.use_external_batch_norm)
            val_loss = model.eval_on(
                    dataset.valid.iter_epoch(args.batch_size, evaluation=True),
                    switch_to_evalmode=model.encoder.use_external_batch_norm)

        try_bsz = (1 if model.encoder.normalization == 'instance' else args.batch_size)
        try_batch = dataset.valid.sample_batch(
            try_bsz, sample_sentence=data.SAMPLE_SENTENCE)
        print(repr(model.try_on(
            try_batch, switch_to_evalmode=model.encoder.use_external_batch_norm)[0]))
        # logger.valid_log(sanity_train_loss, mode='sanity')
        logger.valid_log(val_loss, mode='valid')

        # Save the model if the validation loss is the best we've seen so far.
        if args.save_state:
            logger.save_model_state_dict(model.state_dict(), current=True)
            logger.save_training_state(
                optimizer, args,
                model_state=(model.get_state() if hasattr(model, 'get_state') else None))

        # XXX
        # if model.save_best and False: # not best_val_loss or val_loss['nll_per_w'] < best_val_loss:
        #         logger.save_model_state_dict(model.state_dict())
        #         best_val_loss = val_loss['nll_per_w']

        if args.lr_step_lambda is None and scheduler is not None:
            scheduler.step()
            logger.lr = optimizer.param_groups[0]['lr']

except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')
