import os
import glob
import random
import shutil
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
import torchvision.utils
from torch.utils.data import DataLoader, Dataset, random_split
import torchvision.transforms as transforms
import torchvision.utils as vutils
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from IPython.display import HTML
from generator import Generator
from discriminator import Discriminator
from vae_encoder import VanillaVAEEncoder

device = torch.device("cuda:0" if (torch.cuda.is_available()) else "cpu")
os.environ['KMP_DUPLICATE_LIB_OK']='True'


class CustomImageDataset(Dataset):
    def __init__(self, path, pattern, transform=None):
        self.file_list = glob.glob(os.path.join(path, pattern))
        self.transform = transform

    def __len__(self):
        return len(self.file_list)

    def __getitem__(self, idx):
        output = {}
        data = np.load(self.file_list[idx])
        delta_vmap = torch.tensor(data['delta_vmap'], dtype=torch.float)
        delta_vmap = torch.reshape(delta_vmap, (1, 64, 64))
        output["delta_vmap"] = delta_vmap

        dI = torch.tensor(data["dI"], dtype=torch.float)
        output["dI"] = dI

        dmap = data['dmap']
        # for idx, row in enumerate(dmap):
        #     for jdx, pixel in enumerate(row):
        #         if pixel > 300:
        #             dmap[idx][jdx] = 0

        dmap = torch.tensor(dmap, dtype=torch.float)
        dmap = torch.reshape(dmap, (1, 64, 64))
        nmap = torch.tensor(data['nmap'], dtype=torch.float)
        nmap = nmap.permute(2, 0, 1)
        combined_map = torch.cat((dmap, nmap), dim=0)
        # combined_map = dmap

        if self.transform:
            combined_map = self.transform(combined_map)
        output["combined_map"] = combined_map

        return output


class Data:
    def __init__(self):
        self.dataroot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dcgan_data")
        self.pattern = "d_*.npz"
        self.image_size = 64
        self.batch_size = 16
        self.shuffle = True
        self.num_workers = 2
        self.device = device
        self.transform = transforms.Compose([
            transforms.Resize((64, 64), antialias=True),
            transforms.Normalize(mean=[0.5, 0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5, 0.5]),  # Add data normalization
        ])

    def dataset_prep(self):
        dataset = CustomImageDataset(path=self.dataroot, pattern=self.pattern, transform=self.transform)
        train_size = int(0.8 * len(dataset))
        val_size = len(dataset) - train_size
        train_dataset, val_dataset = random_split(dataset, [train_size, val_size])
        train_dataloader = DataLoader(dataset=train_dataset, batch_size=self.batch_size, shuffle=self.shuffle,
                                      num_workers=self.num_workers)
        val_dataloader = DataLoader(dataset=val_dataset, batch_size=self.batch_size, shuffle=False,
                                    num_workers=self.num_workers)
        return train_dataloader, val_dataloader


class Training:
    def __init__(self, n_epochs: int = 2):
        # set random seed for reproducibility
        self.manualSeed = 1000
        # manualSeed = random.randint(1, 10000) # use if you want new results
        random.seed(self.manualSeed)
        torch.manual_seed(self.manualSeed)
        # in_channels
        self.in_channels = 4
        # rgb image
        self.n_channels = 1
        # latent space input
        self.latent_dims = 64
        # total latent dims including dI values
        self.total_latent_dims = 448
        # size of generator feature map
        self.gen_feature_map_size = 64
        # size of discriminator feature map
        self.dis_feature_map_size = 64
        # labels
        self.labels = {"real": 1.0, "fake": 0.0}
        # loss function - binary cross entropy loss
        self.criterion = nn.BCELoss()
        # loss function for VAE
        self.vae_criterion = nn.KLDivLoss(reduction="batchmean")
        # vae optmizer
        self.vae_optimizer = optim.Adam
        # vae_loss_hyperparameter
        self.vae_loss_scaling_factor = 0.01
        # generator optimizer
        self.gen_optimizer = optim.Adam
        # discriminator optimizer
        self.dis_optimizer = optim.Adam
        # vae encoder learning rate
        self.vae_lr = 0.001
        # generator learning rate
        self.gen_lr = 0.005
        # discriminator learning rate
        self.dis_lr = 0.005
        # optimizer beta
        self.betas = (0.5, 0.999)
        # batch size
        self.batch_size = 16
        # number of epochs
        self.n_epochs = n_epochs
        # device to run model
        self.device = device

    # Suggested by DCGAN paper to initialize the initial generator image with random noises having mean = 0 and std =
    # 0.02
    def weight_initialization(self, gen):
        class_name = gen.__class__.__name__
        if class_name.find("Conv") != -1:
            nn.init.normal_(gen.weight.data, 0.0, 0.02)
        elif class_name.find("BatchNorm") != -1:
            nn.init.normal_(gen.weight.data, 1.0, 0.02)
            nn.init.constant_(gen.bias.data, 0)

    def training(self):
        # initialize vae encoder
        vae_encoder = VanillaVAEEncoder(in_channels=self.in_channels, latent_dim=self.latent_dims)
        # initialize generator
        generator = Generator(self.total_latent_dims, self.gen_feature_map_size,
                              self.n_channels).to(device=self.device)
        generator.apply(self.weight_initialization)
        # initialize discriminator
        discriminator = Discriminator(self.dis_feature_map_size, self.n_channels).to(self.device)
        discriminator.apply(self.weight_initialization)

        fixed_noise = torch.randn(64, self.total_latent_dims, 1, 1, device=self.device)
        criterion = self.criterion
        vae_criterion = self.vae_criterion
        vae_optimizer = self.vae_optimizer(vae_encoder.parameters(), lr=self.vae_lr, betas=self.betas)
        gen_optimizer = self.gen_optimizer(generator.parameters(), lr=self.gen_lr, betas=self.betas)
        dis_optimizer = self.dis_optimizer(discriminator.parameters(), lr=self.dis_lr, betas=self.betas)
        train_dataloader, val_dataloader = Data().dataset_prep()

        # Lists to keep track of progress
        img_list = []
        val_img_list = []
        gen_losses = []
        dis_losses = []
        val_gen_losses = []
        val_dis_losses = []
        iters, train_iters, val_iters = 0, 0, 0

        # create folder to save the generated and real images for comparison
        train_imgs_path = os.path.join(os.getcwd(), "generated_and_real_images", "train_imgs")
        val_imgs_path = os.path.join(os.getcwd(), "generated_and_real_images", "val_imgs")
        for f_path in [train_imgs_path, val_imgs_path]:
            if os.path.exists(f_path):
                shutil.rmtree(f_path)
            os.makedirs(f_path)

        print("Starting Training Loop...")
        # For each epoch
        for epoch in range(self.n_epochs):
            gen_loss, dis_loss, val_gen_loss, val_dis_loss, train_batches, val_batches = 0, 0, 0, 0, 0, 0
            # For each batch in the dataloader
            for mode in ["train", "val"]:
                if mode == "train":
                    loader = train_dataloader
                else:
                    loader = val_dataloader
                    torch.no_grad()

                for i, data in enumerate(loader, 0):

                    ############################
                    # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
                    ###########################
                    # Train with all-real batch
                    discriminator.zero_grad()
                    # Format batch
                    real = data["delta_vmap"].to(device)
                    b_size = real.size(0)
                    label = torch.full((b_size,), self.labels["real"], dtype=torch.float, device=device)
                    # Forward pass real batch through D
                    output = discriminator(real).view(-1)
                    # Calculate loss on all-real batch
                    dis_error_real = criterion(output, label)
                    if mode == "train":
                        # Calculate gradients for D in backward pass
                        dis_error_real.backward()
                    D_x = output.mean().item()

                    # Train with all-fake batch
                    # Generate batch of latent vectors
                    combined_map = data["combined_map"]
                    vae_latent_embedding = vae_encoder.forward(combined_map).to(self.device)
                    vae_latent_embedding = nn.functional.normalize(vae_latent_embedding)
                    di = data["dI"].to(self.device)
                    vae_latent_embedding_with_di = torch.cat((vae_latent_embedding, di), dim=1).to(self.device)
                    vae_latent_embed_vector = torch.reshape(vae_latent_embedding_with_di,
                                                            (b_size, self.total_latent_dims, 1, 1))

                    # Generate fake image batch with G
                    fake = generator(vae_latent_embed_vector)
                    # labels for fake image batch
                    label.fill_(self.labels["fake"])
                    # Classify all fake batch with D
                    output = discriminator(fake.detach()).view(-1)
                    # Calculate D's loss on the all-fake batch
                    dis_error_fake = criterion(output, label)
                    if mode == "train":
                        # Calculate the gradients for this batch, accumulated (summed) with previous gradients
                        dis_error_fake.backward()
                    D_G_z1 = output.mean().item()
                    # Compute error of D as sum over the fake and the real batches
                    dis_error = dis_error_real + dis_error_fake
                    if mode == "train":
                        # Update D
                        dis_optimizer.step()

                    ############################
                    # (2) Update G network: maximize log(D(G(z)))
                    ###########################
                    generator.zero_grad()
                    label.fill_(self.labels["real"])  # fake labels are real for generator cost
                    # Since we just updated D, perform another forward pass of all-fake batch through D
                    output = discriminator(fake).view(-1)
                    # Calculate G's loss based on this output
                    gen_error = criterion(output, label)
                    # Update VAE Encoder to generate better latent vectors
                    delta_vmaps = data["delta_vmap"].to(self.device)
                    # vae error between fake and delta_maps
                    softmax_fake = nn.functional.softmax(fake, dim=2)
                    softmax_d_vmaps = nn.functional.softmax(delta_vmaps, dim=2)
                    vae_error = vae_criterion(softmax_fake, softmax_d_vmaps)
                    # total error
                    total_error = gen_error + (- self.vae_loss_scaling_factor * vae_error)
                    D_G_z2 = output.mean().item()
                    if mode == "train":
                        # Calculate gradients for G
                        total_error.backward()
                        # Update G
                        gen_optimizer.step()
                        # vae_error.backward(retain_graph=True)
                        # Update VAE
                        vae_optimizer.step()

                    # Output training stats
                    if i % 50 == 0:
                        # Output training stats
                        if i % 50 == 0:
                            print(f"[{epoch}/{self.n_epochs}][{i}/{len(loader)}]\tVAE Loss: {vae_error.item():.4f}"
                                  f"\tLoss_D: {dis_error.item():.4f} \tLoss_G: {gen_error.item():.4f}\tD(x): {D_x:.4f}"
                                  f"\tD(G(z)): {D_G_z1:.4f} / {D_G_z2:.4f}")

                    if mode == "train":
                        gen_loss += gen_error.item()
                        dis_loss += dis_error.item()
                        train_batches += 1

                        # save fake images generated from the embedding vector to compare with corresponding real images
                        if epoch == self.n_epochs - 1:
                            for i in range(len(fake)):
                                torchvision.utils.save_image(tensor=fake[i, :, :, :],
                                                             fp=os.path.join(train_imgs_path, f"{train_iters*self.batch_size + i:03d}_gen.jpg"))
                                torchvision.utils.save_image(tensor=real[i, :, :, :],
                                                             fp=os.path.join(train_imgs_path, f"{train_iters*self.batch_size + i:03d}_real.jpg"))
                            train_iters += 1

                        # Check how the generator is doing by saving G's output on fixed_noise
                        if (iters % 500 == 0) or ((epoch == self.n_epochs - 1) and (i == len(train_dataloader) - 1)):
                            with torch.no_grad():
                                fake = generator(fixed_noise).detach().cpu()
                            img_list.append(vutils.make_grid(fake, padding=2, normalize=True))
                    else:
                        val_gen_loss += gen_error.item()
                        val_dis_loss += dis_error.item()
                        val_batches += 1

                        # save fake images generated from the embedding vector to compare with corresponding real images
                        if epoch == self.n_epochs - 1:
                            for i in range(len(fake)):
                                torchvision.utils.save_image(tensor=fake[i, :, :, :],
                                                             fp=os.path.join(val_imgs_path, f"{val_iters*self.batch_size + i:03d}_gen.jpg"))
                                torchvision.utils.save_image(tensor=real[i, :, :, :],
                                                             fp=os.path.join(val_imgs_path, f"{val_iters*self.batch_size + i:03d}_real.jpg"))
                            val_iters += 1

                        # Check how the generator is doing by saving G's output on fixed_noise
                        if (iters % 500 == 0) or ((epoch == self.n_epochs - 1) and (i == len(loader) - 1)):
                            fake = generator(fixed_noise).detach().cpu()
                            val_img_list.append(vutils.make_grid(fake, padding=2, normalize=True))

                    iters += 1

                if mode == "train":
                    # Save Losses for plotting later
                    gen_losses.append(gen_loss/train_batches)
                    dis_losses.append(dis_loss/train_batches)
                else:
                    # Save Losses for plotting later
                    val_gen_losses.append(val_gen_loss/val_batches)
                    val_dis_losses.append(val_dis_loss/val_batches)

        return img_list, val_img_list, gen_losses, dis_losses, val_gen_losses, val_dis_losses

    def execute(self):
        return self.training()


class VisualizeModel:
    def __init__(self, n_epochs):
        self.gen_grid_imgs, self.val_gen_grid_imgs, self.gen_losses, \
            self.val_dis_losses, self.val_gen_losses, self.dis_losses = Training(n_epochs=n_epochs).execute()

    def gen_dis_loss_training(self):
        plt.figure(figsize=(10, 5))
        plt.title("Generator and Discriminator Loss During Training")
        plt.plot(self.gen_losses, label="G - Train")
        plt.plot(self.val_gen_losses, label="G - Validation", linestyle="--")
        plt.plot(self.dis_losses, label="D - Train")
        plt.plot(self.val_dis_losses, label="D - Validation", linestyle="--")
        plt.xlabel("Epochs")
        plt.ylabel("Loss")
        plt.legend()
        plt.show()

    def gen_output(self):
        fig = plt.figure(figsize=(8, 8))
        plt.axis("off")
        ims = [[plt.imshow(np.transpose(i, (1, 2, 0)), animated=True)] for i in self.gen_grid_imgs]
        ani = animation.ArtistAnimation(fig, ims, interval=1000, repeat_delay=1000, blit=True)

        HTML(ani.to_jshtml())
        plt.show()
        ani.save("idc_vae_dcgan_animation.gif", writer="pillow", fps=1)


def main():
    viz_model = VisualizeModel(n_epochs=10)
    viz_model.gen_output()
    # viz_model.gen_dis_loss_training()


if __name__ == "__main__":
    main()
