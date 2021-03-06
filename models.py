from __future__ import print_function

import codecs
import copy
import pprint
from collections import defaultdict

import numpy as np
from nltk.tokenize import word_tokenize

import torch
import torch.nn as nn
import torch.optim as optim

def eval_all_but_batchnorm(module): # XXX temporary solution
    module.training = any([isinstance(module, bn) for bn in
        [nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d]])

    for child in module.children():
        eval_all_but_batchnorm(child)

    return module

def reset_batchnorms(module, momentum, zero_var=False): # XXX temporary solution
    if any([isinstance(module, bn) for bn in
        [nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d]]):
        module.running_mean.fill_(0)
        if zero_var:
            module.running_var.fill_(0)
        else:
            module.running_var.fill_(1)
        module.momentum = momentum

    for child in module.children():
        reset_batchnorms(child, momentum, zero_var)

    return module

def apply_to_batchnorm(module, fun): # XXX temporary solution
    if any([isinstance(module, bn) for bn in
        [nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d]]):
        fun(module)

    for child in module.children():
        apply_to_batchnorm(child, fun)

    return module


class NoopLayer(nn.Module):
    def forward(self, x):
        return x

norm_protos = {'batch': lambda c: nn.BatchNorm1d(c, momentum=0.1),
               'instance': lambda c: nn.InstanceNorm1d(c),
                None: lambda c: NoopLayer()}

pooling_protos = {'max': lambda kernel_size: nn.MaxPool1d(kernel_size),
                  'avg': lambda kernel_size: nn.AvgPool1d(kernel_size)}


class ExpandConv1d(nn.Module):
    def __init__(self, *args, **kwargs):
        super(ExpandConv1d, self).__init__()
        self.conv1d = nn.Conv1d(*args, **kwargs)
        self.out_channels = self.conv1d.out_channels // 2
        print("USING EXPAND RESIDUAL")

    def forward(self, x):
        # Output of conv1d: (N,Cout,Lout)
        x = self.conv1d(x)
        bsz, _, l = x.size()
        x = x.view(bsz, self.out_channels, 2, l).transpose(2, 3).contiguous()
        return x.view(bsz, self.out_channels, 2 * l).contiguous()


class JustExpand(nn.Module):
    def __init__(self, out_channels):
        super(JustExpand, self).__init__()
        self.out_channels = out_channels

    def forward(self, x):
        bsz, _, l = x.size()
        x = x.view(bsz, self.out_channels, 2, l).transpose(2, 3).contiguous()
        return x.view(bsz, self.out_channels, 2 * l).contiguous()


class ExpandResidual(nn.Module):
    def __init__(self, layer_proto, layer2_proto=None, out_relu=True,
                 normalization='batch', residual_connection=True,
                 dropout=0.0):
        super(ExpandResidual, self).__init__()
        self.layer1 = layer_proto()
        self.relu = nn.ReLU()
        self.layer2 = layer2_proto() if layer2_proto else layer_proto()
        self.out_relu = out_relu
        self.normalization = normalization
        self.residual_connection = residual_connection
        self.dropout = nn.Dropout(p=dropout)
        self.expand = JustExpand(out_channels=self.layer1.out_channels // 2)

        # TODO Keep old 'bn' name for backward compat when loading params
        self.bn1 = norm_protos[normalization](self.num_channels(self.layer1))
        self.bn2 = norm_protos[normalization](self.num_channels(self.layer2))

    def num_channels(self, layer):
        if type(layer) in [nn.Conv1d, ExpandConv1d]:
            return layer.out_channels
        #if type(layer) is ExpandConv1d:
        #    return layer.conv1d.out_channels
        #elif type(layer) is nn.Conv1d:
        #    return layer.out_channels
        elif type(layer) is nn.Linear:
            return layer.weight.size(0)
        else:
            raise ValueError('Unsupported layer type.')

    def forward(self, x, norm1=None, norm2=None):
        x = self.dropout(x)
        residual = x
        out = self.layer1(x)

        if norm1 is not None:
            out = norm1(out)
        else:
            out = self.bn1(out)

        out = self.expand(out)

        if self.residual_connection:
            residual = self.expand(torch.cat([residual,residual],dim=1))

        out = self.relu(out)
        out = self.layer2(out)

        if norm2 is not None:
            out = norm2(out)
        else:
            out = self.bn2(out)

        if self.residual_connection:
            out += residual
        if self.out_relu:
            out = self.relu(out)
        return out


class Residual(nn.Module):
    def __init__(self, layer_proto, layer2_proto=None, out_relu=True,
                 normalization='batch', residual_connection=True, dropout=0.0):
        super(Residual, self).__init__()
        self.layer1 = layer_proto()
        self.relu = nn.ReLU()
        self.layer2 = layer2_proto() if layer2_proto else layer_proto()
        self.out_relu = out_relu
        self.normalization = normalization
        self.residual_connection = residual_connection
        self.dropout = nn.Dropout(p=dropout)

        # TODO Keep old 'bn' name for backward compat when loading params
        self.bn1 = norm_protos[normalization](self.num_channels(self.layer1))
        self.bn2 = norm_protos[normalization](self.num_channels(self.layer2))

    def num_channels(self, layer):
        if type(layer) in [nn.Conv1d, ExpandConv1d]:
            return layer.out_channels
        elif type(layer) is nn.Linear:
            return layer.weight.size(0)
        else:
            raise ValueError('Unsupported layer type.')

    def forward(self, x, norm1=None, norm2=None):
        x = self.dropout(x)
        residual = x
        out = self.layer1(x)

        if norm1 is not None:
            out = norm1(out)
        else:
            out = self.bn1(out)

        out = self.relu(out)
        out = self.layer2(out)

        if norm2 is not None:
            out = norm2(out)
        else:
            out = self.bn2(out)

        if self.residual_connection:
            out += residual
        if self.out_relu:
            out = self.relu(out)
        return out


class Gate(nn.Module):
    def __init__(self, num_channels):
        super(Gate, self).__init__()
        self.sigmoid = nn.Sigmoid()
        self.num_channels = num_channels
        self.inflation_conv = nn.Conv1d(num_channels, 2*num_channels,
                kernel_size=1)

    def forward(self, x):
        """
            x: (batch_size x num_channels x dim)
        """
        x = self.inflation_conv(x)
        out = x[:, :num_channels, :]
        gates = self.sigmoid(x[:, num_channels:, :])

        return gates * out


class CNNEncoder(nn.Module):
    def __init__(self, n, emsize, normalization,
                 use_init_projection=False, dim=None,
                 use_linear_layers=True, compress_channels=None,
                 dropout=0.0, mod_r=2, pooling="max", use_gates=False,
                 use_external_batch_norm=False, external_batch_max_r=None):
        super(CNNEncoder, self).__init__()
        self.n = n
        self.emsize = emsize
        self.dim = dim or emsize
        self.mod_r = mod_r
        self.latent_dim = self.dim * int(2 ** self.mod_r)
        self.external_batch_max_r = external_batch_max_r
        self.normalization = normalization
        self.init_projection = (
                nn.Linear(self.dim, self.dim) if use_init_projection else None)
        assert mod_r in [0, 1, 2], 'mod_r should be either 0, 1 or 2'
        assert n % 2 == 0, 'n should be a multiple of 2'
        conv_kwargs = dict(kernel_size=3, stride=1, padding=1, bias=False)
        conv_proto = lambda: nn.Conv1d(self.dim, self.dim, **conv_kwargs)
        linear_proto = lambda: nn.Linear(self.latent_dim, self.latent_dim)
        def residual_list(proto, k, normalization,
                          last_relu=True, dropout=0.0):
            res_list = [Residual(
                    proto,
                    out_relu=(last_relu or i < k-1),
                    normalization=normalization,
                    dropout=dropout)
                for i in xrange(k)]
            return res_list

        self.prefix = nn.Sequential(
                *(residual_list(
                    conv_proto, n//2, normalization, dropout=dropout) + \
                ([Gate(num_channels=self.dim)] if use_gates else [])))

        self.recurrent = nn.Sequential(
                *(residual_list(conv_proto, n//2,
                    normalization=None if normalization == 'batch' and use_external_batch_norm else normalization) + \
                ([Gate(num_channels=self.dim)] if use_gates else []) + \
                [pooling_protos[pooling](kernel_size=2)]))

        if use_external_batch_norm:
            assert external_batch_max_r is not None, \
                    'to use external batchnorm in reccurent part' + \
                    ' you need to specify maximum number of recurrent steps'
            self.rec_batchnorm_list = nn.ModuleList([
                nn.BatchNorm1d(self.dim, momentum=0.1)
                for _ in range((2 * (n // 2)) * external_batch_max_r)])

        self.use_external_batch_norm = use_external_batch_norm

        # compress_channels should be a power of 2
        self.use_linear_layers = use_linear_layers
        self.compress_channels = compress_channels

        #TODO: normalization in postfix XXX
        if self.compress_channels:
            assert 2**int(np.log2(compress_channels)) == compress_channels
            em_in = self.dim
            postfix_layers = []
            while em_in > self.compress_channels:
                postfix_layers.append(Residual(
                    lambda: nn.Conv1d(em_in, em_in, **conv_kwargs),
                    lambda: nn.Conv1d(em_in, em_in // 2, **conv_kwargs),
                    normalization=normalization,
                    residual_connection=False,
                    out_relu=(em_in // 2 > self.compress_channels)))
                em_in = em_in // 2
            self.postfix = nn.Sequential(*(postfix_layers))
        elif self.use_linear_layers:
            postfix_layers = residual_list(
                linear_proto, n//2, normalization,
                last_relu=False, dropout=dropout)
            self.postfix = nn.Sequential(*postfix_layers)
        else:
            self.postfix = None

    def embed(self, x):
        raise NotImplementedError

    def forward(self, x, r=None, sent_len=None, embed=True):
        """
            x: (batch_size, seq_len) if embed == True
               (batch_size, seq_len, emsize) if embed == False
            r: 1 (int)
            sent_len: batch_size (int)
        """
        assert (r is None) ^ (sent_len is None)
        bsz = x.size(0)

        if sent_len is not None:
            rs = [max(0, int(np.ceil(np.log2(len_))) - self.mod_r) for len_ in sent_len]
        else:
            rs = bsz * [max(0, r - self.mod_r)]

        rs_sorted_list = sorted(rs)
        rs = torch.LongTensor(rs)
        if x.is_cuda:
            rs = rs.cuda()
        rs_sorted, perm = torch.sort(rs)
        _, rev_perm = torch.sort(perm)

        if embed == True:
            assert x.size(1) >= 4
            x = self.embed(x)

        if self.init_projection is not None:
            x = self.init_projection(x)

        x = x.transpose(1, 2)
        x = x[perm]
        x = self.prefix(x)

        max_r = rs_sorted_list[-1]
        assert self.external_batch_max_r is None or max_r <= self.external_batch_max_r
        i = 0
        rec_num = 0
        outputs = []
        while i < bsz:
            if rs_sorted_list[i] == rec_num:
                outputs.append(x[i, :, :int(2 ** self.mod_r)])
                i += 1
            else:
                if self.use_external_batch_norm:
                    offset = self.n * rec_num
                    for j, layer in enumerate(self.recurrent):
                        if isinstance(layer, Residual):
                            bn1 = self.rec_batchnorm_list[offset + 2 * j]
                            bn2 = self.rec_batchnorm_list[offset + 2 * j + 1]
                            x = layer(x, norm1=bn1, norm2=bn2)
                        else:
                            x = layer(x)
                else:
                    x = self.recurrent(x)
                rec_num += 1

        if rs_sorted_list[0] != rs_sorted_list[-1]:
            x = torch.stack(outputs, dim=0)
        x = x[rev_perm]

        if self.compress_channels:
            return self.postfix(x).view(bsz, -1)
        elif self.use_linear_layers:
            return self.postfix(x.view(bsz, -1))
        else:
            assert self.postfix is None
            return x.view(bsz, -1)


class WordCNNEncoder(CNNEncoder):
    def __init__(self, **kwargs):
        super(WordCNNEncoder, self).__init__(**kwargs)


class ByteCNNEncoder(CNNEncoder):
    def __init__(self, emsize, vocab_size, padding_idx, **kwargs):
        super(ByteCNNEncoder, self).__init__(emsize=emsize, **kwargs)
        self.embedding = nn.Embedding(vocab_size, emsize, padding_idx=padding_idx)

    def embed(self, x):
        return self.embedding(x)


class ByteCNNDecoder(nn.Module):
    def __init__(self, n, dim, vocab_size, normalization,
                 use_linear_layers=True, compress_channels=None,
                 output_embeddings_init=None,
                 use_external_batch_norm=False, external_batch_max_r=None,
                 output_emb_tie_weights=True, expand_residual=False,
                 mod_r=2):
        super(ByteCNNDecoder, self).__init__()
        self.n = n
        self.dim = dim
        self.mod_r = mod_r
        self.latent_dim = dim * int(2 ** self.mod_r)
        self.vocab_size = vocab_size
        self.external_batch_max_r = external_batch_max_r
        self.normalization = normalization
        assert mod_r in [0, 1, 2], 'mod_r should be either 0, 1 or 2'
        assert n % 2 == 0, 'n should be a multiple of 2'
        conv_kwargs = dict(kernel_size=3, stride=1, padding=1, bias=False)
        conv_proto = lambda: nn.Conv1d(dim, dim, **conv_kwargs)
        expand_proto = lambda: ExpandConv1d(dim, dim*2, **conv_kwargs)
        linear_proto = lambda: nn.Linear(self.latent_dim, self.latent_dim)
        residual_list = lambda proto, k, normalization, last_relu=True: [
                Residual(proto, out_relu=(last_relu or i < k-1),
                         normalization=normalization)
                for i in xrange(k)]

        # Set a prefix layer
        self.use_linear_layers = use_linear_layers
        self.compress_channels = compress_channels

        if self.compress_channels:
            em_in = self.compress_channels
            prefix_layers = []
            while em_in < self.dim:
                prefix_layers.append(Residual(
                    lambda: nn.Conv1d(em_in, em_in, **conv_kwargs),
                    lambda: nn.Conv1d(em_in, em_in * 2, **conv_kwargs),
                    normalization=normalization,
                    residual_connection=False))
                em_in = em_in * 2
            self.prefix = nn.Sequential(*(prefix_layers))

        elif self.use_linear_layers:
            self.prefix = nn.Sequential(
                *(residual_list(linear_proto, n//2, normalization=normalization)))
        else:
            self.prefix = None

        # Set a recurrent layer
        if expand_residual:
            self.recurrent = nn.Sequential(
                *([ExpandResidual(lambda: nn.Conv1d(dim, dim*2, **conv_kwargs), conv_proto, out_relu=True,
                            normalization=(normalization == 'batch' and None if use_external_batch_norm else normalization),
                            )] + \
                  residual_list(conv_proto, n//2-1,
                                normalization=(normalization == 'batch' and None if use_external_batch_norm else normalization))))
        else:
            self.recurrent = nn.Sequential(
                *([Residual(expand_proto, conv_proto, out_relu=True,
                            normalization=(normalization == 'batch' and None if use_external_batch_norm else normalization),
                            residual_connection=False)] + \
                  residual_list(conv_proto, n//2-1,
                                normalization=(normalization == 'batch' and None if use_external_batch_norm else normalization))))

        self.postfix = nn.Sequential(
                *(residual_list(conv_proto, n//2,
                                normalization=normalization,
                                last_relu=(output_embeddings_init is not None))))
        if use_external_batch_norm:
            assert external_batch_max_r is not None, \
                    'to use external batchnorm in reccurent part' + \
                    ' you need to specify maximum number of recurrent steps'
            self.rec_batchnorm_list = nn.ModuleList([
                nn.BatchNorm1d(dim, momentum=0.1) for _ in range(2 * (n // 2) * external_batch_max_r)])

        self.use_external_batch_norm = use_external_batch_norm

        self.output_embedding = None
        if output_embeddings_init and output_emb_tie_weights:
            self.output_embedding = nn.Linear(dim, vocab_size)
            self.output_embedding.weight = output_embeddings_init.weight
        elif dim != vocab_size:
            self.output_embedding = nn.Linear(dim, vocab_size)

    def forward(self, x, r):
        if self.compress_channels:
            x = x.view(x.size(0), self.compress_channels, int(2 ** self.mod_r))
            x = self.prefix(x)
        elif self.use_linear_layers:
            x = self.prefix(x)
            x = x.view(x.size(0), self.dim, int(2 ** self.mod_r))
        else:
            assert self.prefix is None
            x = x.view(x.size(0), self.dim, int(2 ** self.mod_r))

        assert self.external_batch_max_r is None or r <= self.external_batch_max_r
        for rec_num in xrange(r - self.mod_r):
            if not self.use_external_batch_norm:
                x = self.recurrent(x)
            else:
                offset = 2 * (self.n // 2) * rec_num
                for i, layer in enumerate(self.recurrent):
                    if isinstance(layer, Residual):
                        bn1 = self.rec_batchnorm_list[offset + 2 * i]
                        bn2 = self.rec_batchnorm_list[offset + 2 * i + 1]
                        x = layer(x, norm1=bn1, norm2=bn2)
                    else:
                        x = layer(x)

        x = self.postfix(x)

        if self.output_embedding:
            x = self.output_embedding(x.transpose(1,2).contiguous()). \
                     transpose(1,2).contiguous()

        return x


class ByteCNN(nn.Module):
    save_best = True
    def __init__(self, n=8, emsize=256, vocab_size=256,
            encoder_norm='batch', decoder_norm='batch',
            ignore_index=-1, eos=0,
            use_linear_layers=True, compress_channels=None,
            use_output_embeddings=False, dropout=0.0, unroll_r=None,
            use_external_batch_norm=False, external_batch_max_r=None,
            divide_recursive_grads=False,
            output_emb_tie_weights=True,
            expand_residual=False, backprop_every=1, sub_batchsize=None,
            mod_r=2):
        super(ByteCNN, self).__init__()
        self.n = n
        self.emsize = emsize
        assert encoder_norm in norm_protos
        assert decoder_norm in norm_protos
        self.encoder = ByteCNNEncoder(
                n=n,
                emsize=emsize,
                vocab_size=vocab_size,
                normalization=encoder_norm,
                padding_idx=(ignore_index if ignore_index >= 0 else None),
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels,
                dropout=dropout,
                use_external_batch_norm=use_external_batch_norm,
                external_batch_max_r=external_batch_max_r,
                mod_r=mod_r)

        self.decoder = ByteCNNDecoder(n, self.encoder.dim, vocab_size,
                normalization=decoder_norm,
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels,
                output_embeddings_init=(self.encoder.embedding if use_output_embeddings else None),
                use_external_batch_norm=use_external_batch_norm, external_batch_max_r=external_batch_max_r,
                output_emb_tie_weights=output_emb_tie_weights,
                expand_residual=expand_residual,
                mod_r=mod_r)

        self.log_softmax = nn.LogSoftmax()
        self.criterion = nn.CrossEntropyLoss(ignore_index=ignore_index, size_average=False)
        self.eos = eos
        self.unroll_r = None
        if unroll_r:
            self.unroll(unroll_r)
        self.divide_recursive_grads = divide_recursive_grads
        self.backprop_every = backprop_every
        self.sub_batchsize = sub_batchsize

    def forward(self, x):
        x = self.encoder(x, r)
        x = self.decoder(x, r)
        return self.log_softmax(x)

    def num_recurrences(self, x):
        if self.unroll_r:
            return 1 + 2  # 2 will be subtracted from r during forward()
        rfloat = np.log2(x.size(-1))
        r = int(rfloat)
        assert float(r) == rfloat, x.size(-1)
        return r

    def unroll(self, r, clone_weights=False):
        if self.unroll_r:
            raise ValueError('Model already unrolled.')
        self.unroll_r = r
        self.encoder.recurrent = nn.Sequential(
            *[self.encoder.recurrent] + [copy.deepcopy(self.encoder.recurrent) \
              for _ in range(r-1)])
        self.decoder.recurrent = nn.Sequential(
            *[self.decoder.recurrent] + [copy.deepcopy(self.decoder.recurrent) \
              for _ in range(r-1)])

        if not clone_weights:
            [l.reset_parameters() for l in self.encoder.recurrent \
             if hasattr(l, 'reset_parameters')]
            [l.reset_parameters() for l in self.decoder.recurrent \
             if hasattr(l, 'reset_parameters')]

    def get_state(self):
        return dict(unroll_r=self.unroll_r)

    def load_state(self, state):
        self.unroll_r = state.get('unroll_r', None)

    def _encode_decode(self, src, tgt, r_tgt=None):
        r_src = self.num_recurrences(src)
        r_tgt = self.num_recurrences(tgt) if r_tgt is None else r_tgt
        features = self.encoder(src, r_src)
        return self.decoder(features, r_tgt)

    def train_on(self, batch_iterator, optimizer, scheduler=None, logger=None, clip=None):
        self.train()
        losses = []
        errs = []
        for batch, batch_tuple in enumerate(batch_iterator):
            src, tgt, lengths = (batch_tuple + (None,))[:3]

            if self.sub_batchsize:
                assert lengths is not None

                err_rate = 0
                loss = 0.0
                num_samples = 0

                for i in range(0, src.size(0), self.sub_batchsize):
                    src_ = src[i:(i+self.sub_batchsize), :lengths[i]]
                    tgt_ = tgt[i:(i+self.sub_batchsize), :lengths[i]]

                    if i % self.backprop_every == 0:
                        self.zero_grad()
                        sub_loss = 0.0
                        sub_mask_sum = 0

                    decoded = self._encode_decode(src_, tgt_)
                    mask = (tgt_ != self.criterion.ignore_index)
                    sub_mask_sum += mask.sum().float().item()
                    sub_loss += self.criterion(
                        decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                        tgt_.view(-1))

                    if batch % self.backprop_every == (self.backprop_every - 1):
                        loss += sub_loss.item()
                        (sub_loss / sub_mask_sum).backward()
                        if self.divide_recursive_grads:
                            r = 1.0 * self.num_recurrences(src_)
                            for p in self.encoder.recurrent.parameters():
                                p.grad /= (r - 1)
                            for p in self.decoder.recurrent.parameters():
                                p.grad /= (r - 1)

                        if clip:
                            total_norm = nn.utils.clip_grad_norm_(self.parameters(), clip)
                        optimizer.step()

                    _, predictions = decoded.max(dim=1)
                    err_rate += 100. * (predictions[mask] != tgt_[mask]).sum().item()
                    num_samples += mask.sum().item()

                losses.append(loss)
                errs.append(err_rate)
                logger.train_log(batch, {'loss': losses[-1], 'err': errs[-1],},
                                 named_params=self.named_parameters, num_samples=num_samples)
            else:
                if batch % self.backprop_every == 0:
                    self.zero_grad()

                decoded = self._encode_decode(src, tgt)
                mask = (tgt != self.criterion.ignore_index)
                loss = self.criterion(
                    decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                    tgt.view(-1))
                (loss / mask.sum().float()).backward()

                if batch % self.backprop_every == (self.backprop_every - 1):
                    if self.divide_recursive_grads:
                        r = 1.0 * self.num_recurrences(src)
                        for p in self.encoder.recurrent.parameters():
                            p.grad /= (r - 1)
                        for p in self.decoder.recurrent.parameters():
                            p.grad /= (r - 1)

                    if clip:
                        total_norm = nn.utils.clip_grad_norm_(self.parameters(), clip)
                    optimizer.step()

                _, predictions = decoded.max(dim=1)
                err_rate = 100. * (predictions[mask] != tgt[mask]).sum()
                losses.append(loss.item())
                errs.append(err_rate.item())
                num_samples = mask.sum().item()
                logger.train_log(
                        batch,
                        {'loss': losses[-1], 'err': errs[-1]},
                        named_params=self.named_parameters,
                        num_samples=num_samples)

        if scheduler is not None:
            scheduler.step()
            logger.lr = optimizer.param_groups[0]['lr']

        return losses, errs

    def eval_on(self, batch_iterator, switch_to_evalmode=True):
        #self.eval() if switch_to_evalmode else self.train()
        if switch_to_evalmode:
            self.eval()
        else:
            eval_all_but_batchnorm(self)

        errs = 0
        samples = 0
        total_loss = 0
        batch_cnt = 0

        errs_by_len = np.zeros(11)
        errs_by_len_samples = np.zeros(11)

        with torch.no_grad():
            for batch, batch_tuple in enumerate(batch_iterator):
                src, tgt, lengths = (batch_tuple + (None,))[:3]
                decoded = self._encode_decode(src, tgt)
                total_loss += self.criterion(
                    decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                    tgt.view(-1)).item()

                _, predictions = decoded.max(dim=1)
                mask = (tgt != self.criterion.ignore_index)
                errs += (predictions[mask] != tgt[mask]).sum().item()
                samples += mask.sum().item()
                batch_cnt += 1

                if lengths is not None:
                    # Update errors by length
                    for preds, tgts, m, l2 in zip(predictions, tgt, mask, lengths):
                        e = (preds[m] != tgts[m]).sum().item()
                        l = int(np.log2(l2))
                        errs_by_len[l] += e
                        errs_by_len_samples[l] += m.sum().item()

        if errs_by_len.sum() > 0:
            lengthwise_acc = {}
            for l, (this_errs, this_samples) in enumerate(zip(errs_by_len, errs_by_len_samples)):
                if this_samples > 0:
                    lengthwise_acc[2**l] = str(int(100 - 100. * this_errs / this_samples)) + '%'
            print(sorted(lengthwise_acc.items()))
        return {'loss': total_loss / samples, #batch_cnt,
                'acc': 100 - 100. * errs / samples}

    def lengthwise_eval_on(self, bsz, dataset, num_batches_for_stats=100):
        assert bsz is not None

        def double_running(bn): bn.running_mean *= 2 ; bn.running_var *= 2
        def normalize_running(bn, i): bn.running_mean /= (1.0*i) ; bn.running_var /= (1.0*i)

        errs = 0
        samples = 0
        total_loss = 0
        batch_cnt = 0
        lengthwise_acc = {}
        with torch.no_grad():
            for L in dataset.sentence_lengths():
                this_samples = 0
                this_errs = 0
                self.train()

                # We want to aggregate perfect averages instead of running averages
                # To do so we set momentum=0.5 and multiply by 2.0 all params every iteration
                # Lastly normalize the parameters by (i+1) - the number of batches
                #
                # NOTE If not halving/normalizing, then zero_var=False
                reset_batchnorms(self, momentum=0.5, zero_var=True)

                # Note that evaluation= controls shuffling
                for i, (src, tgt) in enumerate(dataset.iter_epoch(bsz=bsz, evaluation=True, len_=L)):
                    self._encode_decode(src, tgt)
                    apply_to_batchnorm(self, double_running)
                    if i == num_batches_for_stats:
                        break
                apply_to_batchnorm(self, lambda bn: normalize_running(bn, (i+1)))
                self.eval()
                for (src, tgt) in dataset.iter_epoch(bsz=bsz, evaluation=True, len_=L):
                    decoded = self._encode_decode(src, tgt)
                    total_loss += self.criterion(
                        decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                        tgt.view(-1)).item()

                    _, predictions = decoded.max(dim=1)
                    mask = (tgt != self.criterion.ignore_index)
                    errs += (predictions[mask] != tgt[mask]).sum().item()
                    samples += mask.sum().item()
                    batch_cnt += 1
                    this_errs += (predictions[mask] != tgt[mask]).sum().item()
                    this_samples += mask.sum().item()
                lengthwise_acc[L] = str(int(100 - 100. * this_errs / this_samples)) + '%'
        print(sorted(lengthwise_acc.items()))
        return {'loss': total_loss / samples,
                'acc': 100 - 100. * errs / samples,}

    def try_on(self, batch_iterator, switch_to_evalmode=True, r_tgt=None,
               return_outputs=False):
        #self.eval() if switch_to_evalmode else self.train()
        if switch_to_evalmode:
            self.eval()
        else:
            eval_all_but_batchnorm(self)

        outputs = []
        predicted = []
        with torch.no_grad():
            for (src, tgt) in batch_iterator:
                decoded = self._encode_decode(src, tgt, r_tgt=r_tgt)
                _, predictions = decoded.max(dim=1)
                if return_outputs:
                    outputs.append(decoded.cpu().numpy())

                # Make into strings and append to decoded
                for pred in predictions:
                    pred = list(pred.cpu().numpy())
                    pred = pred[:pred.index(self.eos)] if self.eos in pred else pred
                    pred = ''.join([chr(c) for c in pred])
                    predicted.append(pred)
        return (predicted, outputs) if return_outputs else predicted

    @staticmethod
    def load_model(path):
        """Load a model"""
        model_pt = os.path.join(path, 'model.pt')
        model_info = os.path.join(path, 'model.info')

        with open(model_info, 'r') as f:
            p = defaultdict(str)
            p.update(dict(line.strip().split('=', 1) for line in f))

        # Read and pop one by one, then raise if something's left
        model_class = eval(p['model_class'])
        del p['model_class']
        model_kwargs = eval("dict(%s)" % p['model_kwargs'])
        del p['model_kwargs']
        if len(p) > 0:
            raise ValueError('Unknown model params: ' + ', '.join(p.keys()))

        assert p['model_class'] == 'ByteCNN', \
            'Tried to load %s as ByteCNN' % p['model_class']
        model = model_class(**model_kwargs)
        with open(model_pt, 'rb') as f:
            model.load_state_dict(torch.load(f))
        return model

##############################################################################
#TODO: migrate to PyTorch 0.4

class ConceptRNN(nn.Module):
    save_best = True
    def __init__(self, n=8, emsize=256, vocab_size=256, rnn_hid_size=None,
            encoder_norm='batch', decoder_norm='batch',
            ignore_index=-1, eos=0,
            use_linear_layers=True, compress_channels=None,
            use_output_embeddings=False):  ## XXX Check default emsize
        super(ConceptRNN, self).__init__()
        self.n = n
        self.emsize = emsize
        assert encoder_norm in norm_protos
        assert decoder_norm in norm_protos
        self.encoder = ByteCNNEncoder(n, emsize, vocab_size,
                normalization=encoder_norm,
                padding_idx=(ignore_index if ignore_index >= 0 else None),
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels)

        self.decoder = ByteCNNDecoder(n, emsize, vocab_size,
                normalization=decoder_norm,
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels,
                output_embeddings_init=None)

        self.rnn = nn.LSTM(
                input_size=2*emsize,
                hidden_size=rnn_hid_size or emsize,
                batch_first=True)

        self.output_projection = None
        if self.rnn.hidden_size != vocab_size or use_output_embeddings:
            self.output_projection = nn.Linear(self.rnn.hidden_size, vocab_size)

        self.log_softmax = nn.LogSoftmax()
        self.criterion = nn.CrossEntropyLoss(ignore_index=ignore_index)
        self.eos = eos

    def forward(self, x):
        #r = self.num_recurrences(x)
        #x = self.encoder(x, r)
        #x = self.decoder(x, r)
        #return self.log_softmax(x)
        raise NotImplementedError  # TODO

    def num_recurrences(self, x):
        rfloat = np.log2(x.size(-1))
        r = int(rfloat)
        assert float(r) == rfloat, x.size(-1)
        return r

    def _encode_decode(self, src, tgt, r_tgt=None):
        r_src = self.num_recurrences(src)
        r_tgt = self.num_recurrences(tgt) if r_tgt is None else r_tgt
        features = self.encoder(src, r_src)
        inflated = self.decoder(features, r_tgt).transpose(1, 2)
        eos = tgt.new(torch.Size([1, tgt.size(1)])).fill_(self.eos)
        embedded_tokens = torch.cat([
            self.encoder.embedding(eos),
            self.encoder.embedding(tgt[:-1])])
        rnn_input = torch.cat([embedded_tokens, inflated], dim=2)
        decoded = self.rnn(rnn_input)[0]
        if self.output_projection is not None:
            decoded = self.output_projection(decoded)
        return decoded

    def train_on(self, batch_iterator, optimizer, scheduler=None, logger=None):
        self.train()
        losses = []
        errs = []
        for batch, (src, tgt) in enumerate(batch_iterator):
            self.zero_grad()
            decoded = self._encode_decode(src, tgt)
            loss = self.criterion(
                decoded.contiguous().view(-1, decoded.size(2)),
                tgt.view(-1))
            loss.backward()
            optimizer.step()

            _, predictions = decoded.max(dim=2)
            mask = (tgt != self.criterion.ignore_index)
            err_rate = 100. * (predictions[mask] != tgt[mask]).sum() / mask.sum()
            losses.append(loss.item())
            errs.append(err_rate.item())
            logger.train_log(batch, {'loss': losses[-1], 'acc': 100. - errs[-1],},
                             named_params=self.named_parameters)

            if scheduler is not None:
                scheduler.step()
                logger.lr = optimizer.param_groups[0]['lr']

        return losses, errs

    def eval_on(self, batch_iterator, switch_to_evalmode=True):
        self.eval() if switch_to_evalmode else self.train()
        errs = 0
        samples = 0
        total_loss = 0
        batch_cnt = 0
        with torch.no_grad():
            for (src, tgt) in batch_iterator:
                decoded = self._encode_decode(src, tgt)
                total_loss += self.criterion(
                    decoded.contiguous().view(-1, decoded.size(2)),
                    tgt.view(-1)).item()

                _, predictions = decoded.max(dim=2)
                mask = (tgt != self.criterion.ignore_index)
                errs += (predictions[mask] != tgt[mask]).sum().item()
                samples += mask.sum().item()
                batch_cnt += 1
        return {'loss': total_loss / batch_cnt,
                'acc': 100 - 100. * errs / samples,}

    def try_on(self, batch_iterator, switch_to_evalmode=True, r_tgt=None):
        self.eval() if switch_to_evalmode else self.train()
        predicted = []
        with torch.no_grad():
            for (src, tgt) in batch_iterator:
                decoded = self._encode_decode(src, tgt, r_tgt=r_tgt)
                _, predictions = decoded.max(dim=2)

                # Make into strings and append to decoded
                for pred in predictions:
                    pred = list(pred.cpu().numpy())
                    pred = pred[:pred.index(self.eos)] if self.eos in pred else pred
                    pred = repr(''.join([chr(c) for c in pred]))
                    predicted.append(pred)
        return predicted

    @staticmethod
    def load_model(path):
        """Load a model"""
        model_pt = os.path.join(path, 'model.pt')
        model_info = os.path.join(path, 'model.info')

        with open(model_info, 'r') as f:
            p = defaultdict(str)
            p.update(dict(line.strip().split('=', 1) for line in f))

        # Read and pop one by one, then raise if something's left
        model_class = eval(p['model_class'])
        del p['model_class']
        model_kwargs = eval("dict(%s)" % p['model_kwargs'])
        del p['model_kwargs']
        if len(p) > 0:
            raise ValueError('Unknown model params: ' + ', '.join(p.keys()))

        assert p['model_class'] == 'ByteCNN', \
            'Tried to load %s as ByteCNN' % p['model_class']
        model = model_class(**model_kwargs)
        with open(model_pt, 'rb') as f:
            model.load_state_dict(torch.load(f))
        return model


class VAEByteCNN(nn.Module):
    save_best = True
    def __init__(self, n=8, emsize=256, vocab_size=256,
                 encoder_norm='batch', decoder_norm='batch',
                 ignore_index=-1, eos=0,
                 use_linear_layers=True, compress_channels=None,
                 use_output_embeddings=False, unroll_r=None,
                 kl_weight_init=1e-5, kl_weight_end=1.0,
                 kl_increment_start=None, kl_increment=None):
        super(VAEByteCNN, self).__init__()
        self.n = n
        self.emsize = emsize
        assert encoder_norm in norm_protos
        assert decoder_norm in norm_protos
        self.encoder = ByteCNNEncoder(n, emsize, vocab_size,
                normalization=encoder_norm,
                padding_idx=(ignore_index if ignore_index >= 0 else None),
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels)
        self.decoder = ByteCNNDecoder(n, emsize, vocab_size,
                normalization=decoder_norm,
                use_linear_layers=use_linear_layers,
                compress_channels=compress_channels,
                output_embeddings_init=(self.encoder.embedding if use_output_embeddings else None))
        self.log_softmax = nn.LogSoftmax()
        self.criterion = nn.CrossEntropyLoss(ignore_index=ignore_index)
        self.eos = eos
        self.projection = nn.Linear(4*emsize, 8*emsize)

        self.kl_weight = kl_weight_init
        self.kl_weight_end = kl_weight_end
        self.kl_increment_start = kl_increment_start
        self.kl_increment = kl_increment
        self.unroll_r = None
        if unroll_r:
            self.unroll(unroll_r)

    def get_state(self):
        return dict(kl_weight=self.kl_weight,
                    kl_increment_start=self.kl_increment_start,
                    unroll_r=self.unroll_r)

    def load_state(self, state):
        self.kl_weight = state['kl_weight']
        self.kl_increment_start = state.get('kl_increment_start', None)
        self.unroll_r = state.get('unroll_r', None)

    def get_features_and_KL(self, mu, log_sigma):
        bs = mu.size(0)
        dim = mu.size(1)
        sigma = torch.exp(log_sigma)
        kl = -0.5 * torch.sum((1.0 + 2.0 * log_sigma - mu**2 - sigma**2) / (bs * dim))
        epsilon = mu.new(bs, dim).normal_()
        features = epsilon * sigma + mu
        return features, kl

    def forward(self, x):
        r = self.encoder.num_recurrences(x)
        x = self.encoder(x, r)
        x = self.decoder(x, r)
        return self.log_softmax(x)

    def num_recurrences(self, x):
        if self.unroll_r:
            return 1 + 2  # 2 will be subtracted from r during forward()
        rfloat = np.log2(x.size(-1))
        r = int(rfloat)
        assert float(r) == rfloat
        return r

    def unroll(self, r, clone_weights=False):
        if self.unroll_r:
            raise ValueError('Model already unrolled.')
        self.unroll_r = r
        self.encoder.recurrent = nn.Sequential(
            *[self.encoder.recurrent] + [copy.deepcopy(self.encoder.recurrent) \
              for _ in range(r-1)])
        self.decoder.recurrent = nn.Sequential(
            *[self.decoder.recurrent] + [copy.deepcopy(self.decoder.recurrent) \
              for _ in range(r-1)])

        if not clone_weights:
            [l.reset_parameters() for l in self.encoder.recurrent \
             if hasattr(l, 'reset_parameters')]
            [l.reset_parameters() for l in self.decoder.recurrent \
             if hasattr(l, 'reset_parameters')]

    def _encode_decode(self, src, tgt, r_tgt=None, first_sample_random=False):
        r_src = self.num_recurrences(src)
        r_tgt = self.num_recurrences(tgt) if r_tgt is None else r_tgt
        dist_params = self.projection(self.encoder(src, r_src))
        mu, log_sigma = dist_params.chunk(2, dim=1)
        log_sigma /= 33.0
        features, kl = self.get_features_and_KL(mu, log_sigma)
        if first_sample_random:
            features[0] = torch.randn(*features[0].size())
        return self.decoder(features, r_tgt), kl

    def train_on(self, batch_iterator, optimizer, scheduler=None, logger=None):
        self.train()
        losses = []
        errs = []
        for batch, (src, tgt) in enumerate(batch_iterator):
            self.zero_grad()
            decoded, kl = self._encode_decode(src, tgt)
            loss = self.criterion(
                decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                tgt.view(-1)) + self.kl_weight * kl
            loss.backward()
            optimizer.step()

            _, predictions = decoded.max(dim=1)
            mask = (tgt != self.criterion.ignore_index)
            err_rate = 100. * (predictions[mask] != tgt[mask]).sum() / mask.sum()
            losses.append(loss.tiem())
            errs.append(err_rate.item())
            logger.train_log(batch, {'loss': losses[-1], 'acc': 100. - errs[-1],
                                     'kl': kl.item(), 'kl_weight': self.kl_weight},
                             named_params=self.named_parameters)

            if self.kl_increment_start > 0:
                self.kl_increment_start -= 1
            else:
                self.kl_weight = min(self.kl_weight_end,
                                     self.kl_weight + self.kl_increment)

            if scheduler is not None:
                scheduler.step()
                logger.lr = optimizer.param_groups[0]['lr']

        return losses, errs

    def eval_on(self, batch_iterator, switch_to_evalmode=True):
        self.eval() if switch_to_evalmode else self.train()
        errs = 0
        samples = 0
        total_loss = 0
        batch_cnt = 0
        with torch.no_grad():
            for batch, (src, tgt) in enumerate(batch_iterator):
                decoded, kl = self._encode_decode(src, tgt)
                total_loss += (self.criterion(
                    decoded.transpose(1, 2).contiguous().view(-1, decoded.size(1)),
                    tgt.view(-1)) + self.kl_weight * kl).item()

                _, predictions = decoded.max(dim=1)
                mask = (tgt != self.criterion.ignore_index)
                errs += (predictions[mask] != tgt[mask]).sum().item()
                samples += mask.sum().item()
                batch_cnt += 1
        return {'loss': total_loss / batch_cnt,
                'acc': 100 - 100. * errs / samples,
                'kl': kl.item(), 'kl_weight': self.kl_weight}

    def try_on(self, batch_iterator, switch_to_evalmode=True, r_tgt=None,
               first_sample_random=False, return_outputs=False):
        self.eval() if switch_to_evalmode else self.train()
        outputs = []
        predicted = []
        with torch.no_grad():
            for batch, (src, tgt) in enumerate(batch_iterator):
                decoded, kl = self._encode_decode(src, tgt, r_tgt, first_sample_random=first_sample_random)
                _, predictions = decoded.max(dim=1)
                if return_outputs:
                    outputs.append(decoded.cpu().numpy())

                # Make into strings and append to decoded
                for pred in predictions:
                    pred = list(pred.cpu().numpy())
                    pred = pred[:pred.index(self.eos)] if self.eos in pred else pred
                    pred = ''.join([chr(c) for c in pred])
                    predicted.append(pred)
        return (predicted, outputs) if return_outputs else predicted

    @staticmethod
    def load_model(path):
        """Load a model"""
        model_pt = os.path.join(path, 'model.pt')
        model_info = os.path.join(path, 'model.info')

        with open(model_info, 'r') as f:
            p = defaultdict(str)
            p.update(dict(line.strip().split('=', 1) for line in f))

        # Read and pop one by one, then raise if something's left
        model_class = eval(p['model_class'])
        del p['model_class']
        model_kwargs = eval("dict(%s)" % p['model_kwargs'])
        del p['model_kwargs']
        if len(p) > 0:
            raise ValueError('Unknown model params: ' + ', '.join(p.keys()))

        assert p['model_class'] == 'VAEByteCNN', \
            'Tried to load %s as ByteCNN' % p['model_class']
        model = model_class(**model_kwargs)
        with open(model_pt, 'rb') as f:
            model.load_state_dict(torch.load(f))
        return model
