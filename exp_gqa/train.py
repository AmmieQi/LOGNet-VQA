import os, sys

import torch
import torch.optim as optim
import torch.nn as nn
import numpy as np
import argparse
import time
import logging
from termcolor import colored

import torch.backends.cudnn as cudnn

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
logFormatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
rootLogger = logging.getLogger()

from DataLoader import GQADataLoader
from utils import todevice, MyDataParallel
from validate import validate

from model.LOGNet import LOGNet
from config import cfg, cfg_from_file


def train(cfg):
    logging.info("Create train_loader and val_loader.........")
    train_loader_kwargs = {
        'question_pt': cfg.dataset.train_question,
        'vocab_json': cfg.dataset.vocab_json,
        'object_feature': cfg.dataset.train_object_feature,
        'spatial_feature': cfg.dataset.train_spatial_feature,
        'img_info': cfg.dataset.img_info,
        'train_num': cfg.train.train_num,
        'batch_size': cfg.train.batch_size,
        'num_workers': cfg.num_workers,
        'shuffle': True
    }
    train_loader = GQADataLoader(**train_loader_kwargs)
    logging.info("number of train instances: {}".format(len(train_loader.dataset)))
    val_loader_kwargs = {
        'question_pt': cfg.dataset.val_question,
        'vocab_json': cfg.dataset.vocab_json,
        'object_feature': cfg.dataset.val_object_feature,
        'spatial_feature': cfg.dataset.val_spatial_feature,
        'img_info': cfg.dataset.img_info,
        'val_num': cfg.val.val_num,
        'batch_size': cfg.train.batch_size,
        'num_workers': cfg.num_workers,
        'shuffle': False
    }
    val_loader = GQADataLoader(**val_loader_kwargs)
    logging.info("number of val instances: {}".format(len(val_loader.dataset)))

    logging.info("Create model.........")
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info("device: {}".format(device))

    model_kwargs = {
        'img_size': cfg.train.vision_dim,
        'max_step': cfg.train.net_length,
        'vocab': train_loader.vocab,
    }
    logging.info("net_len: {}".format(model_kwargs['max_step']))
    model_kwargs_tosave = {k: v for k, v in model_kwargs.items() if k != 'vocab'}
    model = LOGNet(cfg, **model_kwargs).to(device)
    pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logging.info('num of params: {}'.format(pytorch_total_params))

    logging.info(model)
    if cfg.train.glove:
        logging.info('load glove vectors')
        model.input_unit.encoder_embed.weight.data.copy_(torch.from_numpy(train_loader.glove_matrix))

    if torch.cuda.device_count() > 1 and cfg.multi_gpus:
        model = model.cuda()
        logging.info("Using {} GPUs".format(torch.cuda.device_count()))
        model = nn.DataParallel(model, device_ids=None)
    ################################################################
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), cfg.train.lr)

    start_epoch = 0
    best_val = 0
    if cfg.train.restore:
        print("Restore checkpoint and optimizer...")
        ckpt = os.path.join(cfg.dataset.save_dir, 'ckpt', 'model.pt')
        ckpt = torch.load(ckpt, map_location=lambda storage, loc: storage)
        start_epoch = ckpt['epoch'] + 1
        model.load_state_dict(ckpt['state_dict'])
        optimizer.load_state_dict(ckpt['optimizer'])
    criterion = nn.CrossEntropyLoss().to(device)
    logging.info("Start training........")
    for epoch in range(start_epoch, cfg.train.max_epochs):
        logging.info('>>>>>> epoch {epoch} <<<<<<'.format(epoch=colored("{}".format(epoch), "green", attrs=["bold"])))
        model.train()
        # set learning rate warmup -> refer https://github.com/KaihuaTang/VQA2.0-Recent-Approachs-2018.pytorch/blob/master/train.py
        optimizer = lr_scheduling(cfg, epoch, optimizer)
        total_acc, count = 0, 0
        total_loss, avg_loss = 0.0, 0.0
        for i, batch in enumerate(train_loader):
            sorted_indices = np.argsort(-batch[4])
            for id_ in range(len(batch)):
                batch[id_] = batch[id_][sorted_indices]
            progress = epoch + i / len(train_loader)
            _, img_ids, answers, *batch_input = [todevice(x, device) for x in batch]
            answers = answers.cuda().squeeze()
            optimizer.zero_grad()
            logits, _ = model(*batch_input, vis=False)
            ##################### loss #####################
            loss = criterion(logits, answers)
            loss.backward()
            total_loss += loss.detach()
            avg_loss = total_loss / (i + 1)
            #################################################
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=8)
            optimizer.step()
            train_acc = batch_accuracy(logits, answers)
            total_acc += train_acc.sum().item()
            count += answers.size(0)
            avg_acc = total_acc / count
            sys.stdout.write(
                "\rProgress = {progress}   ce_loss = {ce_loss}   avg_loss = {avg_loss}    train_acc = {train_acc}    avg_acc = {avg_acc}    exp: {exp_name}".format(
                    progress=colored("{:.3f}".format(progress), "green", attrs=['bold']),
                    ce_loss=colored("{:.4f}".format(loss.item()), "blue", attrs=['bold']),
                    avg_loss=colored("{:.4f}".format(avg_loss), "red", attrs=['bold']),
                    train_acc=colored("{:.4f}".format(train_acc.mean().cpu().numpy()), "blue", attrs=['bold']),
                    avg_acc=colored("{:.4f}".format(avg_acc), "red", attrs=['bold']), exp_name=cfg.exp_name))
            sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
        logging.info("Epoch = %s   avg_loss = %.3f    avg_acc = %.3f" % (epoch, avg_loss, avg_acc))

        if cfg.val.flag:
            output_dir = os.path.join(cfg.dataset.save_dir, 'preds')
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            else:
                assert os.path.isdir(output_dir)
            valid_acc = validate(cfg, model, val_loader, device)
            if valid_acc > best_val:
                best_val = valid_acc
                # Save best model
                ckpt_dir = os.path.join(cfg.dataset.save_dir, 'ckpt')
                if not os.path.exists(ckpt_dir):
                    os.makedirs(ckpt_dir)
                else:
                    assert os.path.isdir(ckpt_dir)
                save_checkpoint(epoch, model, optimizer, model_kwargs_tosave, os.path.join(ckpt_dir, 'model.pt'))
                sys.stdout.write('\n >>>>>> save to %s <<<<<< \n' % (ckpt_dir))
                sys.stdout.flush()

            logging.info('~~~~~~ Valid Accuracy: %.4f ~~~~~~~' % valid_acc)
            sys.stdout.write('~~~~~~ Valid Accuracy: {valid_acc} ~~~~~~~\n'.format(
                valid_acc=colored("{:.4f}".format(valid_acc), "red", attrs=['bold'])))
            sys.stdout.flush()


def lr_scheduling(cfg, epoch, optimizer):
    if epoch < len(cfg.train.gradual_warmup_steps) and cfg.train.schedule_method == 'warm_up':
        all_rl = []
        for param_group in optimizer.param_groups:
            param_group['lr'] = cfg.train.gradual_warmup_steps[epoch]
            all_rl.append(param_group['lr'])
        print('Epoch {:03d}:'.format(epoch), ' Learning Rate: ', set(all_rl))
    elif (epoch in range(cfg.train.epoch_decay_start, cfg.train.max_epochs, cfg.train.lr_decay_step)) and cfg.train.schedule_method \
            == 'warm_up':
        all_rl = []
        for param_group in optimizer.param_groups:
            param_group['lr'] *= cfg.train.lr_decay_rate
            all_rl.append(param_group['lr'])
        print('Epoch {:03d}:'.format(epoch), ' Learning Rate: ', set(all_rl))
    else:
        all_rl = []
        for param_group in optimizer.param_groups:
            all_rl.append(param_group['lr'])
        print('Epoch {:03d}:'.format(epoch), ' Learning Rate: ', set(all_rl))

    return optimizer


def batch_accuracy(pred, gt):
    """ Compute the accuracies for a batch of predictions and answers """
    pred = pred.detach().argmax(1)
    correctness = (pred == gt)
    return correctness.float()


def save_checkpoint(epoch, model, optimizer, model_kwargs, filename):
    state = {
        'epoch': epoch,
        'state_dict': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'model_kwargs': model_kwargs,
    }
    time.sleep(10)
    torch.save(state, filename)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', dest='cfg_file', help='optional config file', default='configs/gqa.yml', type=str)
    args = parser.parse_args()

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)

    if torch.cuda.is_available() and not cfg.multi_gpus:
        torch.cuda.set_device(cfg.gpu_id)

    # make logging.info display into both shell and file
    cfg.dataset.save_dir = os.path.join(cfg.dataset.save_dir, cfg.exp_name)
    if not os.path.exists(cfg.dataset.save_dir):
        os.makedirs(cfg.dataset.save_dir)
    else:
        assert os.path.isdir(cfg.dataset.save_dir)
    log_file = os.path.join(cfg.dataset.save_dir, "log")
    if not cfg.train.restore and not os.path.exists(log_file):
        os.mkdir(log_file)
    else:
        assert os.path.isdir(log_file)

    fileHandler = logging.FileHandler(os.path.join(log_file, 'stdout.log'), 'w+')
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)
    # cfg display
    for k, v in vars(cfg).items():
        logging.info(k + ':' + str(v))
    # join absolute paths of input files
    cfg.dataset.train_question = os.path.join(cfg.dataset.data_dir, cfg.dataset.train_question)
    cfg.dataset.val_question = os.path.join(cfg.dataset.data_dir, cfg.dataset.val_question)
    cfg.dataset.vocab_json = os.path.join(cfg.dataset.data_dir, cfg.dataset.vocab_json)
    cfg.dataset.train_object_feature = os.path.join(cfg.dataset.data_dir, cfg.dataset.train_object_feature)
    cfg.dataset.val_object_feature = os.path.join(cfg.dataset.data_dir, cfg.dataset.val_object_feature)
    cfg.dataset.train_spatial_feature = os.path.join(cfg.dataset.data_dir, cfg.dataset.train_spatial_feature)
    cfg.dataset.val_spatial_feature = os.path.join(cfg.dataset.data_dir, cfg.dataset.val_spatial_feature)
    cfg.dataset.img_info = os.path.join(cfg.dataset.data_dir, cfg.dataset.img_info)

    # set random seed
    def seed_torch(seed=cfg.seed):
        np.random.seed(seed)

    os.environ['PYTHONHASHSEED'] = str(cfg.seed)
    np.random.seed(cfg.seed)
    torch.manual_seed(cfg.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(cfg.seed)
        torch.cuda.manual_seed_all(cfg.seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    seed_torch()

    cfg.train.flag = True
    train(cfg)


if __name__ == '__main__':
    main()
