import argparse
import sys
import os
import yaml
import time
import shutil
import json

import torch
import wandb

sys.path.append("../")
from pycode.config import _C as cfg
from pycode.dataset import RLBench_DMOEBM
from pycode.misc import str2bool, save_checkpoint, save_args
from pycode.model.diffusion import Denoising_Diffusion, Diffusion_Loss

##### parser #####
parser = argparse.ArgumentParser(description='parser for image generator')
parser.add_argument('--config_file', type=str, default='../config/RLBench_Diffusion.yaml', metavar='FILE', help='path to config file')
parser.add_argument('--name', type=str, default="")
parser.add_argument('--add_name', type=str, default="")
parser.add_argument('--log2wandb', type=str2bool, default=True)
parser.add_argument('--device', type=str, default='cuda')
parser.add_argument('--reset_dataset', type=str2bool, default=False)
args = parser.parse_args()

##### config #####
# get cfg data (arg.config_fileが指定されている場合はconfig fileをloadして，pycode.configにかかれている初期値に上書きする．)
if len(args.config_file) > 0:
    print('Loaded configration file {}'.format(args.config_file))
    cfg.merge_from_file(args.config_file)

    # set config_file to wandb (wandbにconfigを保存するために，辞書としてconfigをもっておく)
    with open(args.config_file) as file:
        obj = yaml.safe_load(file)

# いちいちcfg.---って書くのがめんどうなので定義しておく
device = args.device
max_iter = cfg.OUTPUT.MAX_ITER
save_iter = cfg.OUTPUT.SAVE_ITER
eval_iter = cfg.OUTPUT.EVAL_ITER
log_iter = cfg.OUTPUT.LOG_ITER
batch_size = cfg.DATASET.BATCH_SIZE

# configに合わせて保存先の名前を自動で決める．
if args.name == "":
    if cfg.DIFFUSION.TYPE == "normal":
        dir_name = f"Diffusion_step_{cfg.DIFFUSION.STEP}_start_{cfg.DIFFUSION.START}_end_{cfg.DIFFUSION.END}"

        if cfg.DIFFUSION.SIGMA != 1.0:
            dir_name = f"{dir_name}_SIGMA_{cfg.DIFFUSION.SIGMA}"
    elif cfg.DIFFUSION.TYPE == "improved":
            dir_name = f"Improved_Diffusion_frame_{args.frame}_mode_{args.rot_mode}_step_{cfg.DIFFUSION.STEP}_s_{cfg.DIFFUSION.S}_bias_{cfg.DIFFUSION.BIAS}"
    else:
        raise ValueError("invalid diffusion type")

    if cfg.DIFFUSION.IMG_GUIDE < 1.0:
        dir_name = f"{dir_name}_IMG_GUIDE_{cfg.DIFFUSION.IMG_GUIDE}"
# nameが指定されているなら，それを使う
else:
    dir_name = args.name

# 自動でつけた名前にちょっとだけ付け加えたいときに使う
if args.add_name != "":
    dir_name = f"{dir_name}_{args.add_name}"

# 同じ名前ですでに実験していた場合に上書きしていいか確認する
save_dir = os.path.join(cfg.OUTPUT.BASE_DIR, cfg.DATASET.NAME, cfg.DATASET.RLBENCH.TASK_NAME)
save_path = os.path.join(save_dir, dir_name)
print(f"save path:{save_path}")
if os.path.exists(save_path):
    while 1:
        ans = input('The specified output dir is already exists. Overwrite? y or n: ')
        if ans == 'y':
            break
        elif ans == 'n':
            raise ValueError("Please specify correct output dir")
        else:
            print('please type y or n')
else:
    os.makedirs(save_path)


# wandbの設定
if args.log2wandb:
    wandb.login()
    run = wandb.init(project='{}-{}'.format(cfg.DATASET.NAME, cfg.DATASET.RLBENCH.TASK_NAME),
                    config=obj, save_code=True, name=dir_name, dir=save_dir)

# 保存先のdirを作成
model_save_dir = os.path.join(save_path, "model")
log_dir = os.path.join(save_path, 'log')
vis_dir = os.path.join(save_path, 'vis')
os.makedirs(model_save_dir, exist_ok=True)
os.makedirs(vis_dir, exist_ok=True)

# 一応ソースコードを保存しておく（pycode以下のコードを保存してないのであまり意味ない）
shutil.copy(sys.argv[0], save_path)
if len(args.config_file) > 0:
    shutil.copy(os.path.abspath(args.config_file), save_path)

# save args (どのような条件で実験したのかを知るためにargsも一応保存しておくが，なくてもいい)
argsfile_path = os.path.join(save_path, "args.json")
save_args(args,argsfile_path)

# set dataset （データセットのロード：ここは変えてください）
train_dataset  = RLBench_DMOEBM("train", cfg, save_dataset=args.reset_dataset, num_frame=args.frame, rot_mode=rot_mode)
train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=8)

val_dataset = RLBench_DMOEBM("val", cfg, save_dataset=args.reset_dataset, num_frame=args.frame, rot_mode=rot_mode)
val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=100, shuffle=False, num_workers=8)

# set model (モデルのconfigを反映)
conv_dims = cfg.MODEL.CONV_DIMS
enc_depths = cfg.MODEL.ENC_DEPTHS
enc_layers = cfg.MODEL.ENC_LAYERS

dec_depths = cfg.MODEL.DEC_DEPTHS
dec_layers = cfg.MODEL.DEC_LAYERS

extractor_name = cfg.MODEL.EXTRACTOR_NAME
predictor_name = cfg.MODEL.PREDICTOR_NAME

conv_droppath_rate = cfg.MODEL.CONV_DROP_PATH_RATE
atten_dropout_rate = cfg.MODEL.ATTEN_DROPOUT_RATE
query_emb_dim = cfg.MODEL.QUERY_EMB_DIM
num_atten_block = cfg.MODEL.NUM_ATTEN_BLOCK

img_guidance_rate = cfg.DIFFUSION.IMG_GUIDE
max_steps = cfg.DIFFUSION.STEP

# モデルは辞書入力を想定しています．詳細はpycodeの中で確認してください
model = Denoising_Diffusion(["uv","z","rotation","grasp_state"], [2,1,rot_dim,1], cfg.DIFFUSION,
                    dims=conv_dims, enc_depths=enc_depths, enc_layers=enc_layers, dec_depths=dec_depths, dec_layers=dec_layers, 
                    query_emb_dim=query_emb_dim, drop_path_rate=conv_droppath_rate, img_guidance_rate=img_guidance_rate)
model = model.to(device)

# set optimizer and loss
optimizer = torch.optim.Adam(model.parameters(), lr=cfg.OPTIM.LR)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=cfg.OPTIM.SCHEDULER.STEP, gamma=cfg.OPTIM.SCHEDULER.GAMMA)
Train_loss = Diffusion_Loss() #ここも変えてください
# Eval_loss = Diffusion_Loss()

##### start training #####
iteration = 0
start = time.time()
for epoch in range(10000000):
    for data in train_dataloader:

        # 画像と姿勢をGPUへ
        image, pose_dict = data
        image = image.to(device)
        for key in pose_dict.keys():
            pose_dict[key] = pose_dict[key].to(device)
        B,_,_,_ = image.shape

        # diffusionのstepをランダムサンプリング
        t = torch.randint(1, max_steps+1, (B,), device=device).long()

        # モデルの更新
        optimizer.zero_grad()
        pred_dict, noise, info = model(image, pose_dict, t)

        loss, loss_dict = Train_loss(pred_dict, noise)
        loss.backward()
        optimizer.step()
        scheduler.step()

        # log
        if iteration % log_iter == 0:
            end = time.time()
            cost = (end - start) / (iteration+1)
            print(f'Train Iter: {iteration} Cost: {cost:.4g} Loss: {loss_dict["train/loss"]:.4g} uv:{loss_dict["train/uv"]:.4g}')
            
            if args.log2wandb:
                wandb.log(loss_dict, step=iteration)
                wandb.log({"lr": optimizer.param_groups[0]['lr']}, step=iteration)

        # evaluate model
        if iteration % eval_iter == 0:
            with torch.no_grad():

                for data in val_dataloader:
                    model.eval() # 一応モデルを評価用に変更
                    image, pose_dict = data
                    image = image.to(device)
                    for key in pose_dict.keys():
                        pose_dict[key] = pose_dict[key].to(device)
                    t = torch.randint(1, max_steps+1, (100,), device=device).long()

                    optimizer.zero_grad()
                    pred_dict, noise, info = model(image, pose_dict, t)
                    loss, loss_dict = Train_loss(pred_dict, noise, mode="val")
                    print(f'Val Iter: {iteration} Loss: {loss_dict["val/loss"]:.4g} uv:{loss_dict["val/uv"]:.4g}, z:{loss_dict["val/z"]:.4g}, rot:{loss_dict["val/rotation"]:.4g}, grasp:{loss_dict["val/grasp_state"]:.4g}')

                    model.train() # モデルを学習用に戻す
            
                if args.log2wandb:
                    wandb.log(loss_dict, step=iteration)

        # save model（モデルの保存）
        if iteration % save_iter == 0:
            model_save_path = os.path.join(model_save_dir, f"model_iter{str(iteration).zfill(5)}.pth")
            save_checkpoint(model, optimizer, epoch, iteration, model_save_path)

        if iteration == max_iter + 1:
            sys.exit()
        
        iteration += 1