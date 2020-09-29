from torch.backends import cudnn
from utils.logger import setup_logger
from model import make_model
from solver import make_optimizer, WarmupMultiStepLR
from loss import make_loss
from datasets.sampler import RandomIdentitySampler
from datasets.bases import ImageDataset
from datasets.make_dataloader import train_collate_fn
import argparse
from config import cfg
from config import cfg_test
from processor import do_train, do_inference_Pseudo
import random
import torch
import numpy as np
from datasets import make_dataloader_Pseudo, make_dataloader
import os
import os.path as osp
from torch.utils.data import DataLoader
from collections import defaultdict

def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="ReID Baseline Training")
    parser.add_argument(
        "--config_file", default="", help="path to config file", type=str
    )
    parser.add_argument(
        "--config_file_test", default="", help="path to config file", type=str
    )
    parser.add_argument(
        "--data_dir_query", default="", help="dir to the query datasets", type=str
    )
    parser.add_argument(
        "--data_dir_gallery", default="", help="dir to the gallery datasets", type=str
    )
    parser.add_argument("opts", help="Modify config options using the command-line", default=None,
                        nargs=argparse.REMAINDER)

    args = parser.parse_args()

    imgs_dir_query = args.data_dir_query
    imgs_dir_test = args.data_dir_gallery

    if args.config_file_test != "":
        cfg_test.merge_from_file(args.config_file_test)
    cfg_test.freeze()


    if args.config_file_test != "":
        print("Loaded test configuration file {}".format(args.config_file_test))
        with open(args.config_file_test, 'r') as cf:
            config_str = "\n" + cf.read()
            print(config_str)
    print(cfg_test, 'cfg_test')

    train_loader, val_loader_green, val_loader_normal, num_query_green, num_query_normal, num_classes = make_dataloader(cfg_test)
    KNOWN = num_classes
    print("num_class in the custom training: {}".format(KNOWN))

    model = make_model(cfg_test, num_class=num_classes)

    model.load_param(cfg_test.TEST.WEIGHT)
    print('Ready for inference')

    distmat, img_name_q, img_name_g = do_inference_Pseudo(cfg_test, model, val_loader_green, num_query_green)

    print(distmat, 'distmat')
    print('The shape of distmat is: {}'.format(distmat.shape))

    if args.config_file != "":
        cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()

    set_seed(cfg.SOLVER.SEED)

    output_dir = cfg.OUTPUT_DIR
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    logger = setup_logger("reid_baseline", output_dir, if_train=True)
    logger.info("Saving model in the path :{}".format(cfg.OUTPUT_DIR))
    logger.info(args)

    if args.config_file != "":
        logger.info("Loaded configuration file {}".format(args.config_file))
        with open(args.config_file, 'r') as cf:
            config_str = "\n" + cf.read()
            logger.info(config_str)
    logger.info("Running with config:\n{}".format(cfg))


    indexes = np.argwhere(distmat < cfg.MODEL.THRESH)
    logger.info('Model thresh: {}'.format(cfg.MODEL.THRESH))
    logger.info('The number of galleries selected at the beginning: {}'.format(len(indexes)))

    final_index = defaultdict(list)
    gallery_container = set()

    for index in indexes:
        if index[1] not in gallery_container:
            gallery_container.add(index[1])
            final_index[index[1]] = index[0]
        else:
            if distmat[index[0]][index[1]] < distmat[final_index[index[1]]][index[1]]:
                final_index.pop(index[1])
                final_index[index[1]] = index[0]

    logger.info('The number of galleries selected after processing: {}'.format(len(final_index)))
    seletcted_data = []
    pid_container = set()

    for gallery, query in final_index.items():
        pid_container.add(query)
    pid2label = {pid: label for label, pid in enumerate(pid_container)}

    for gallery, query in final_index.items():
        seletcted_data.append((osp.join(imgs_dir_test,
                                        img_name_g[gallery]), pid2label[query] + KNOWN, 1))

    for pid in pid_container:
        seletcted_data.append((osp.join(imgs_dir_query,
                                        img_name_q[pid]), pid2label[pid] + KNOWN,  1))


    logger.info("the Number of Pseudo-seletcted_data is :{}".format(len(seletcted_data)))
    logger.info("the class of Pseudo-label is :{}".format(len(pid_container)))

    train_loader, val_loader, num_query, num_classes, dataset, train_set, train_transforms = make_dataloader_Pseudo(cfg)

    seletcted_set = ImageDataset(seletcted_data, train_transforms)

    new_train_data = train_set + seletcted_set

    train_loader_test = DataLoader(
        new_train_data, batch_size=cfg.SOLVER.IMS_PER_BATCH,
        sampler=RandomIdentitySampler(dataset.train + seletcted_data, cfg.SOLVER.IMS_PER_BATCH,
                                      cfg.DATALOADER.NUM_INSTANCE),
        num_workers=cfg.DATALOADER.NUM_WORKERS, collate_fn=train_collate_fn
    )

    num_classes = KNOWN + len(pid_container)

    model = make_model(cfg, num_class=num_classes)

    loss_func, center_criterion = make_loss(cfg, num_classes=num_classes)

    optimizer, optimizer_center = make_optimizer(cfg, model, center_criterion)
    scheduler = WarmupMultiStepLR(optimizer, cfg.SOLVER.STEPS, cfg.SOLVER.GAMMA,
                                  cfg.SOLVER.WARMUP_FACTOR,
                                  cfg.SOLVER.WARMUP_EPOCHS, cfg.SOLVER.WARMUP_METHOD)

    if cfg.MODEL.PRETRAIN_CHOICE == 'finetune':
        model.load_param_finetune(cfg.MODEL.PRETRAIN_PATH)
        print('Loading pretrained model for finetuning......')

    do_train(
        cfg,
        model,
        center_criterion,
        train_loader_test,
        val_loader,
        optimizer,
        optimizer_center,
        scheduler,  # modify for using self trained model
        loss_func,
        num_query
    )
