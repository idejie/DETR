# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler

import datasets
import util.misc as utils
from datasets import build_dataset, get_coco_api_from_dataset
from engine import evaluate, train_one_epoch
from models import build_model


def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=4, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--lr_drop', default=40, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', default=True,
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")
    parser.add_argument('--teacher', default=False,action='store_true')
    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=15, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')
    parser.add_argument('--nclasses', default=1, type=int)

    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    # * Matcher
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--distill_loss_coef', default=5, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help="Relative classification weight of the no-object class")

    # dataset parameters
    parser.add_argument('--dataset_file', default='ego4d')
    parser.add_argument('--coco_path', type=str, default='data')
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='checkpoints',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='checkpoints/latest.pth', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=8, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=8, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return parser


def main(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    print(args)
    if args.teacher:
        args.output_dir = args.output_dir+'_teacher'
    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    model, criterion, postprocessors = build_model(args)
    trainable_params = filter(lambda p: p.requires_grad, model.parameters())
    # logger.info(model)
    print(f'Trainable parameters: {sum([p.numel() for p in trainable_params])}')
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)

    param_dicts = [
        {"params": [p for n, p in model_without_ddp.named_parameters() if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)
    if args.dataset_file == 'ego4d':
        dataset_train = build_dataset(image_set='train', args=args)
        dataset_mini_val = build_dataset(image_set='mini_val', args=args)
        dataset_val = build_dataset(image_set='val', args=args)

        if args.distributed:
            sampler_train = DistributedSampler(dataset_train)
            sampler_mini_val = DistributedSampler(dataset_mini_val, shuffle=False)
            sampler_val = DistributedSampler(dataset_val, shuffle=False)
        else:
            sampler_train = torch.utils.data.RandomSampler(dataset_train)
            sampler_mini_val = torch.utils.data.SequentialSampler(dataset_mini_val)
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)

        batch_sampler_train = torch.utils.data.BatchSampler(
            sampler_train, args.batch_size, drop_last=True)

        data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                    collate_fn=utils.collate_fn, num_workers=args.num_workers)
        data_loader_mini_val = DataLoader(dataset_mini_val, args.batch_size, sampler=sampler_mini_val,
                                    drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers)
        data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                    drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers)
        if args.dataset_file == "coco_panoptic":
            # We also evaluate AP during panoptic training, on original coco DS
            coco_val = datasets.coco.build("val", args)
            base_ds = get_coco_api_from_dataset(coco_val)
            coco_mini_val = datasets.coco.build("mini_val", args)
            base_ds_mini_val = get_coco_api_from_dataset(coco_mini_val)
        else:
            base_ds = get_coco_api_from_dataset(dataset_val)
            base_ds_mini_val = get_coco_api_from_dataset(dataset_mini_val)


    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    output_dir = Path(args.output_dir)
    if args.resume:
        print('loading checkpoint', args.resume)
        try:
            if args.resume.startswith('https'):
                print('loading checkpoint from url')
                checkpoint = torch.hub.load_state_dict_from_url(
                    args.resume, map_location='cpu', check_hash=True)
                
            else:
                print('loading checkpoint from local')
                checkpoint = torch.load(args.resume, map_location='cpu')
        except:
            print(f"Error while loading checkpoint from [{args.resume}], training from [https]")
            model1  = torch.load('detr-r50-dc5-f0fb7ef5.pth', map_location='cpu')

            num_class = args.nclasses
            model1["model"]["class_embed.weight"].resize_(num_class+1, 256)
            model1["model"]["class_embed.bias"].resize_(num_class+1)
            model1["model"]["query_embed.weight"].resize_(args.num_queries,256)
            torch.save(model1, "detr-r50_test_%d.pth"%num_class)
            checkpoint = torch.load("detr-r50_test_%d.pth"%num_class, map_location='cpu')
        print('loaded checkpoint', args.resume)
        # if args.teacher:
        #     model.load_state_dict({k.replace('module.',''):v for k,v in checkpoint['model'].items()})
        # else:
        model_without_ddp.load_state_dict(checkpoint['model'],strict=False)
        print('loaded model')
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            try:
                optimizer.load_state_dict(checkpoint['optimizer'])
                lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            except:
                pass
            args.start_epoch = checkpoint['epoch'] + 1
            evaluate('mini_val',model, criterion, postprocessors,
                                              data_loader_mini_val, base_ds_mini_val, device, args.output_dir)

    if args.eval:
        if args.teacher:
            val_stats_T, _ = evaluate('mini_val_T',model, criterion, postprocessors,
                                              data_loader_mini_val, base_ds_mini_val, device, args.output_dir,teacher=True)
            test_stats_T, _ = evaluate('val_T',model, criterion, postprocessors,
                                                data_loader_val, base_ds, device, args.output_dir,teacher=True)
        val_stats, _ = evaluate('mini_val',model, criterion, postprocessors,
                                              data_loader_mini_val, base_ds_mini_val, device, args.output_dir)
        test_stats, coco_evaluator = evaluate('val',model, criterion, postprocessors,
                                              data_loader_val, base_ds, device, args.output_dir)
        if args.output_dir:
            utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
        return
    # test_stats, coco_evaluator = evaluate(
    #         model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir
    #     )
    print("Start training")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            sampler_train.set_epoch(epoch)
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer, device, epoch,
            args.clip_max_norm,teacher=args.teacher)
        lr_scheduler.step()
        if args.output_dir:
            checkpoint_paths = [output_dir / 'checkpoint.pth']
            # extra checkpoint before LR drop and every 100 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)
       
        if args.teacher:
            mini_val_stats_T, coco_evaluator_T = evaluate('mini_val_T',model, criterion, postprocessors,
                                              data_loader_mini_val, base_ds_mini_val, device, args.output_dir,teacher=True)
        mini_val_stats, coco_evaluator = evaluate('mini_val',model, criterion, postprocessors,
                                              data_loader_mini_val, base_ds_mini_val, device, args.output_dir)

        
        if (epoch+1) %10 != 0:
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        **{f'mini_val_{k}': v for k, v in mini_val_stats.items()},
                        'epoch': epoch,
                        'n_parameters': n_parameters}
        else:
            test_stats_T, coco_evaluator_T = evaluate('val_T',
            model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir,teacher=True
            )
            test_stats, coco_evaluator = evaluate('val',
            model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir
            )

            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                        **{f'mini_val_{k}': v for k, v in mini_val_stats.items()},
                        **{f'val_{k}': v for k, v in test_stats.items()},
                        'epoch': epoch,
                        'n_parameters': n_parameters}
            

        if args.output_dir and utils.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")

            # for evaluation logs
            if coco_evaluator is not None:
                (output_dir / 'eval').mkdir(exist_ok=True)
                if "bbox" in coco_evaluator.coco_eval:
                    filenames = ['latest.pth']
                    if epoch % 50 == 0:
                        filenames.append(f'{epoch:03}.pth')
                    for name in filenames:
                        torch.save(coco_evaluator.coco_eval["bbox"].eval,
                                   output_dir / "eval" / name)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)