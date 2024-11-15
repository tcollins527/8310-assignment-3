# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset
import torchvision
import torchvision.transforms as transforms
import numpy as np
import os
import struct
import pandas as pd
from sklearn.model_selection import train_test_split
import requests
import io

# Set device to GPU if available, else CPU, to optimize training speed
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Load pre-trained model from a specified path
model = torch.load('/model.pth')

# Function to read image data
def read_images(filename):
    with open(filename, 'rb') as f:
        magic, num_images, rows, cols = struct.unpack('>IIII', f.read(16))
        images = np.frombuffer(f.read(), dtype=np.uint8)
        images = images.reshape(num_images, 1, rows, cols)
        images = images.astype(np.float32) / 255.0
    return images

# Function to read label data
def read_labels(filename):
    with open(filename, 'rb') as f:
        magic, num_labels = struct.unpack('>II', f.read(8))
        labels = np.frombuffer(f.read(), dtype=np.uint8)
        labels = labels.astype(np.int64)
    return labels

# Define file paths for training and testing data
train_images_file = 'FashionMNIST/raw/train-images-idx3-ubyte'
train_labels_file = 'FashionMNIST/raw/train-labels-idx1-ubyte'
test_images_file = 'FashionMNIST/raw/t10k-images-idx3-ubyte'
test_labels_file = 'FashionMNIST/raw/t10k-labels-idx1-ubyte'

# Load the training & testing data
train_images = read_images(train_images_file)
train_labels = read_labels(train_labels_file)
test_images = read_images(test_images_file)
test_labels = read_labels(test_labels_file)

class CustomMNIST(Dataset):
    def __init__(self, images, labels):
        self.images = images
        self.labels = labels
    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        image = self.images[idx]
        label = self.labels[idx]
        image = torch.tensor(image, dtype=torch.float32)
        label = torch.tensor(label, dtype=torch.long)

        return image, label

# Custom DataLoader to handle batch loading
class CustomDataLoader:
    def __init__(self, dataset, batch_size=64, shuffle=True):
        self.dataset = dataset
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.num_samples = len(dataset)
        self.indices = np.arange(self.num_samples)
        self.current_idx = 0

    def __iter__(self):
        self.current_idx = 0
        if self.shuffle:
            np.random.shuffle(self.indices)
        return self

    def __next__(self):
        if self.current_idx >= self.num_samples:
            raise StopIteration

        batch_indices = self.indices[self.current_idx:self.current_idx + self.batch_size]
        batch = [self.dataset[idx] for idx in batch_indices]

        batch_images, batch_labels = zip(*batch)

        batch_images = torch.stack(batch_images)
        batch_labels = torch.stack(batch_labels)

        self.current_idx += self.batch_size

        return batch_images, batch_labels

    def __len__(self):
        return (self.num_samples + self.batch_size - 1) // self.batch_size

# Initialize datasets --> loaders
train_dataset = CustomMNIST(train_images, train_labels)
test_dataset = CustomMNIST(test_images, test_labels)
train_dataloader = CustomDataLoader(train_dataset, batch_size=64, shuffle=True)
test_dataloader = CustomDataLoader(test_dataset, batch_size=64, shuffle=False)

class Residual(nn.Module):
    """The Residual block of ResNet models."""
    def __init__(self, in_channels, out_channels, use_1x1conv=False, strides=1):
        super(Residual, self).__init__()
        self.conv1 = nn.Conv2d(
            in_channels, out_channels, kernel_size=3, padding=1, stride=strides)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(
            out_channels, out_channels, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(out_channels)
        if use_1x1conv:
            self.conv3 = nn.Conv2d(
                in_channels, out_channels, kernel_size=1, stride=strides)
        else:
            self.conv3 = None

    def forward(self, X):
        Y = F.relu(self.bn1(self.conv1(X)))
        Y = self.bn2(self.conv2(Y))
        if self.conv3:
            X = self.conv3(X)
        Y += X
        return F.relu(Y)

class ResNet(nn.Module):
    def __init__(self, arch, num_classes=10):
        super(ResNet, self).__init__()
        self.in_channels = 64
        self.conv1 = nn.Conv2d(
            1, self.in_channels, kernel_size=7, stride=2, padding=3)
        self.bn1 = nn.BatchNorm2d(self.in_channels)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(
            kernel_size=3, stride=2, padding=1)
        self.layers = self._make_layers(arch)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(self.in_channels, num_classes)

    def _make_layers(self, arch):
        layers = []
        for num_blocks, out_channels in arch:
            strides = 1 if len(layers) == 0 else 2
            layers.append(
                self._make_layer(out_channels, num_blocks, strides))
        return nn.Sequential(*layers)

    def _make_layer(self, out_channels, num_blocks, strides):
        layers = []
        layers.append(
            Residual(self.in_channels, out_channels, use_1x1conv=True, strides=strides))
        self.in_channels = out_channels
        for _ in range(1, num_blocks):
            layers.append(
                Residual(self.in_channels, out_channels))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.maxpool(x)
        x = self.layers(x)
        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)
        return x

class ResNet18(ResNet):
    def __init__(self, num_classes=10):
        super(ResNet18, self).__init__(
            arch=((2, 64), (2, 128), (2, 256), (2, 512)),
            num_classes=num_classes)

# Function to train the model on the training dataset
def train_loop(dataloader, model, loss_fn, optimizer):
    size = len(dataloader.dataset)
    num_batches = len(dataloader)
    print_interval = max(1, num_batches // 5)

    model.train()
    for batch_idx, (X, y) in enumerate(dataloader):
        X, y = X.to(device), y.to(device)

        pred = model(X)
        loss = loss_fn(pred, y)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_idx % print_interval == 0:
            loss_value = loss.item()
            current = batch_idx * len(X)
            print(f"loss: {loss_value:>7f}  [{current:>5d}/{size:>5d}]")

# Function to evaluate the model on the test dataset
def test_loop(dataloader, model, loss_fn):
    model.eval()
    size = len(dataloader.dataset)
    num_batches = len(dataloader)
    test_loss, correct = 0, 0

    with torch.no_grad():
        for X, y in dataloader:
            X, y = X.to(device), y.to(device)

            pred = model(X)
            test_loss += loss_fn(pred, y).item()
            correct += (pred.argmax(1) == y).type(torch.float).sum().item()

    test_loss /= num_batches
    correct /= size
    print(f"Test Error:\n Accuracy: {(100*correct):>0.1f}%, Avg loss: {test_loss:>8f}\n")

# Function to train the entire network with multiple epochs
def train_net(model, train_dataloader, test_dataloader, epochs=20, learning_rate=1e-3):
    loss_fn = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)

    for t in range(epochs):
        try:
            print(f"Epoch {model.EPOCH + t + 1}\n-------------------------------")
        except AttributeError:
            print(f"Epoch {t + 1}\n-------------------------------")
        train_loop(train_dataloader, model, loss_fn, optimizer)
        test_loop(test_dataloader, model, loss_fn)
    print("Done!")

    try:
        model.EPOCH += epochs
    except AttributeError:
        model.EPOCH = epochs

    return model

# Train the model
model = train_net(model, train_dataloader, test_dataloader, epochs=5, learning_rate=1e-3)

# Save the trained model's state to a file
torch.save(model.state_dict(), 'model.pth')

