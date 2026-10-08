"""
U²-Net integration with PyTorch to remove the background and detect foreground objects. 
See official paper in https://arxiv.org/pdf/2005.09007 for more details.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

class ConvBNReLU(nn.Module):
    """
    Convolutional layer followed by Batch Normalization and ReLU activation.
    """
    def __init__(self, in_channels, out_channels, kernel_size=3, dilation=1):
        super(ConvBNReLU, self).__init__()

        self.layer = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=dilation, dilation=dilation),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x):
        return self.layer(x)
    

def upsample(x, target):
    """
    Upsample the input tensor to the specified target tensor size using bilinear interpolation.
    """
    return F.interpolate(x, size=target.shape[2:], mode='bilinear', align_corners=True)
    
class RSU7(nn.Module):
    def __init__(self, in_channels=3, mid_channels=32, out_channels=64):
        super(RSU7, self).__init__()

        self.convbnreluin = ConvBNReLU(in_channels=in_channels, out_channels=out_channels)
        self.convbnrelu1 = ConvBNReLU(in_channels=out_channels, out_channels=mid_channels)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu2 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu3 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu4 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool4 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu5 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool5 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu6 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.convbnrelu7 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=2) # Last layer with dilation = 2

        self.convbnrelu6d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu5d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu4d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu3d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu2d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu1d = ConvBNReLU(in_channels=2*mid_channels, out_channels=out_channels)

    def forward(self, x):
        hxin = self.convbnreluin(x)
        hx1 = self.convbnrelu1(hxin)
        hx2 = self.convbnrelu2(self.pool1(hx1))
        hx3 = self.convbnrelu3(self.pool2(hx2))
        hx4 = self.convbnrelu4(self.pool3(hx3))
        hx5 = self.convbnrelu5(self.pool4(hx4))
        hx6 = self.convbnrelu6(self.pool5(hx5))

        hx7 = self.convbnrelu7(hx6)

        hx6d = self.convbnrelu6d(torch.cat((hx7, hx6), dim=1))
        hx5d = self.convbnrelu5d(torch.cat((upsample(hx6d, hx5), hx5), dim=1))
        hx4d = self.convbnrelu4d(torch.cat((upsample(hx5d, hx4), hx4), dim=1))
        hx3d = self.convbnrelu3d(torch.cat((upsample(hx4d, hx3), hx3), dim=1))
        hx2d = self.convbnrelu2d(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.convbnrelu1d(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        return hx1d + hxin
    
class RSU6(nn.Module):
    def __init__(self, in_channels=64, mid_channels=32, out_channels=128):
        super(RSU6, self).__init__()

        self.convbnreluin = ConvBNReLU(in_channels=in_channels, out_channels=out_channels)
        self.convbnrelu1 = ConvBNReLU(in_channels=out_channels, out_channels=mid_channels)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu2 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu3 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu4 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool4 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu5 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.convbnrelu6 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=2) # Last layer with dilation = 2

        self.convbnrelu5d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu4d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu3d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu2d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu1d = ConvBNReLU(in_channels=2*mid_channels, out_channels=out_channels)

    def forward(self, x):
        hxin = self.convbnreluin(x)
        hx1 = self.convbnrelu1(hxin)
        hx2 = self.convbnrelu2(self.pool1(hx1))
        hx3 = self.convbnrelu3(self.pool2(hx2))
        hx4 = self.convbnrelu4(self.pool3(hx3))
        hx5 = self.convbnrelu5(self.pool4(hx4))

        hx6 = self.convbnrelu6(hx5)
        
        hx5d = self.convbnrelu5d(torch.cat((hx6, hx5), dim=1))
        hx4d = self.convbnrelu4d(torch.cat((upsample(hx5d, hx4), hx4), dim=1))
        hx3d = self.convbnrelu3d(torch.cat((upsample(hx4d, hx3), hx3), dim=1))
        hx2d = self.convbnrelu2d(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.convbnrelu1d(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        return hx1d + hxin

class RSU5(nn.Module):
    def __init__(self, in_channels=128, mid_channels=64, out_channels=256):
        super(RSU5, self).__init__()

        self.convbnreluin = ConvBNReLU(in_channels=in_channels, out_channels=out_channels)
        self.convbnrelu1 = ConvBNReLU(in_channels=out_channels, out_channels=mid_channels)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu2 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu3 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)
    
        self.convbnrelu4 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.convbnrelu5 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=2) # Last layer with dilation = 2

        self.convbnrelu4d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu3d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu2d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu1d = ConvBNReLU(in_channels=2*mid_channels, out_channels=out_channels)

    def forward(self, x):
        hxin = self.convbnreluin(x)
        hx1 = self.convbnrelu1(hxin)
        hx2 = self.convbnrelu2(self.pool1(hx1))
        hx3 = self.convbnrelu3(self.pool2(hx2))
        hx4 = self.convbnrelu4(self.pool3(hx3))

        hx5 = self.convbnrelu5(hx4)

        hx4d = self.convbnrelu4d(torch.cat((hx5, hx4), dim=1))
        hx3d = self.convbnrelu3d(torch.cat((upsample(hx4d, hx3), hx3), dim=1))
        hx2d = self.convbnrelu2d(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.convbnrelu1d(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        return hx1d + hxin

class RSU4(nn.Module):
    def __init__(self, in_channels=256, mid_channels=128, out_channels=512):
        super(RSU4, self).__init__()

        self.convbnreluin = ConvBNReLU(in_channels=in_channels, out_channels=out_channels)
        self.convbnrelu1 = ConvBNReLU(in_channels=out_channels, out_channels=mid_channels)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu2 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)

        self.convbnrelu3 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels)
        self.convbnrelu4 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=2)

        self.convbnrelu3d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu2d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels)
        self.convbnrelu1d = ConvBNReLU(in_channels=2*mid_channels, out_channels=out_channels)

    def forward(self, x):
        hxin = self.convbnreluin(x)
        hx1 = self.convbnrelu1(hxin)
        hx2 = self.convbnrelu2(self.pool1(hx1))
        hx3 = self.convbnrelu3(self.pool2(hx2))

        hx4 = self.convbnrelu4(hx3)

        hx3d = self.convbnrelu3d(torch.cat((hx4, hx3), dim=1))
        hx2d = self.convbnrelu2d(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.convbnrelu1d(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        return hx1d + hxin


class RSU4F(nn.Module):
    def __init__(self, in_channels=512, mid_channels=256, out_channels=512):
        super(RSU4F, self).__init__()

        self.convbnreluin = ConvBNReLU(in_channels=in_channels, out_channels=out_channels)
        self.convbnrelu1 = ConvBNReLU(in_channels=out_channels, out_channels=mid_channels)
        self.convbnrelu2 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=2)
        self.convbnrelu3 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=4)
        self.convbnrelu4 = ConvBNReLU(in_channels=mid_channels, out_channels=mid_channels, dilation=8)

        self.convbnrelu3d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels, dilation=4)
        self.convbnrelu2d = ConvBNReLU(in_channels=2*mid_channels, out_channels=mid_channels, dilation=2)
        self.convbnrelu1d = ConvBNReLU(in_channels=2*mid_channels, out_channels=out_channels)

    def forward(self, x):
        hxin = self.convbnreluin(x)
        hx1 = self.convbnrelu1(hxin)
        hx2 = self.convbnrelu2(hx1)
        hx3 = self.convbnrelu3(hx2)
        hx4 = self.convbnrelu4(hx3)

        hx3d = self.convbnrelu3d(torch.cat((hx4, hx3), dim=1))
        hx2d = self.convbnrelu2d(torch.cat((hx3d, hx2), dim=1))
        hx1d = self.convbnrelu1d(torch.cat((hx2d, hx1), dim=1))

        return hx1d + hxin

class U2Net(nn.Module):
    def __init__(self, in_channels=3, out_channels=1):
        super(U2Net, self).__init__()

        self.enc1 = RSU7(in_channels=in_channels)
        self.pool1 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc2 = RSU6()
        self.pool2 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc3 = RSU5()
        self.pool3 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc4 = RSU4()
        self.pool4 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc5 = RSU4F()
        self.pool5 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc6 = RSU4F()

        self.dec5 = RSU4F(in_channels=1024)
        self.dec4 = RSU4(in_channels=1024, out_channels=256)
        self.dec3 = RSU5(in_channels=512, out_channels=128)
        self.dec2 = RSU6(in_channels=256, out_channels=64)
        self.dec1 = RSU7(in_channels=128, mid_channels=16)

        self.side6 = nn.Conv2d(in_channels=512, out_channels=out_channels, kernel_size=3, padding=1)
        self.side5 = nn.Conv2d(in_channels=512, out_channels=out_channels, kernel_size=3, padding=1)
        self.side4 = nn.Conv2d(in_channels=256, out_channels=out_channels, kernel_size=3, padding=1)
        self.side3 = nn.Conv2d(in_channels=128, out_channels=out_channels, kernel_size=3, padding=1)
        self.side2 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side1 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)

        self.outconv = nn.Conv2d(in_channels=6, out_channels=out_channels, kernel_size=1)


    def forward(self, x):
        # === Encoder ===
        hx1 = self.enc1(x)
        hx2 = self.enc2(self.pool1(hx1))
        hx3 = self.enc3(self.pool2(hx2))
        hx4 = self.enc4(self.pool3(hx3))
        hx5 = self.enc5(self.pool4(hx4))
        hx6 = self.enc6(self.pool5(hx5))

        # === Decoder ===
        hx5d = self.dec5(torch.cat((upsample(hx6, hx5), hx5), dim=1))
        hx4d = self.dec4(torch.cat((upsample(hx5d, hx4), hx4), dim=1))
        hx3d = self.dec3(torch.cat((upsample(hx4d, hx3), hx3), dim=1))
        hx2d = self.dec2(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.dec1(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        # === Side Outputs ===
        sup1 = self.side1(hx1d)
        sup2 = upsample(self.side2(hx2d), sup1)
        sup3 = upsample(self.side3(hx3d), sup1)
        sup4 = upsample(self.side4(hx4d), sup1)
        sup5 = upsample(self.side5(hx5d), sup1)
        sup6 = upsample(self.side6(hx6), sup1)

        # === Fusion ===
        sup0 = self.outconv(torch.cat((sup6, sup5, sup4, sup3, sup2, sup1), dim=1))

        # Logits as outputs for deep supervision using BCEWithLogitsLoss (numerically stable)
        return sup0, sup1, sup2, sup3, sup4, sup5, sup6 

class U2NetP(nn.Module):
    def __init__(self, in_channels=3, out_channels=1):
        super(U2NetP, self).__init__()

        self.enc1 = RSU7(in_channels=in_channels, mid_channels=16, out_channels=64)
        self.pool1 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc2 = RSU6(in_channels=64, mid_channels=16, out_channels=64)
        self.pool2 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc3 = RSU5(in_channels=64, mid_channels=16, out_channels=64)
        self.pool3 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc4 = RSU4(in_channels=64, mid_channels=16, out_channels=64)
        self.pool4 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc5 = RSU4F(in_channels=64, mid_channels=16, out_channels=64)
        self.pool5 = nn.MaxPool2d(2, stride=2, ceil_mode=True)

        self.enc6 = RSU4F(in_channels=64, mid_channels=16, out_channels=64)

        self.dec5 = RSU4F(in_channels=128, mid_channels=16, out_channels=64)
        self.dec4 = RSU4(in_channels=128, mid_channels=16, out_channels=64)
        self.dec3 = RSU5(in_channels=128, mid_channels=16, out_channels=64)
        self.dec2 = RSU6(in_channels=128, mid_channels=16, out_channels=64)
        self.dec1 = RSU7(in_channels=128, mid_channels=16, out_channels=64)

        self.side6 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side5 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side4 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side3 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side2 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)
        self.side1 = nn.Conv2d(in_channels=64, out_channels=out_channels, kernel_size=3, padding=1)

        self.outconv = nn.Conv2d(in_channels=6, out_channels=out_channels, kernel_size=1)


    def forward(self, x):
        # === Encoder ===
        hx1 = self.enc1(x)
        hx2 = self.enc2(self.pool1(hx1))
        hx3 = self.enc3(self.pool2(hx2))
        hx4 = self.enc4(self.pool3(hx3))
        hx5 = self.enc5(self.pool4(hx4))
        hx6 = self.enc6(self.pool5(hx5))

        # === Decoder ===
        hx5d = self.dec5(torch.cat((upsample(hx6, hx5), hx5), dim=1))
        hx4d = self.dec4(torch.cat((upsample(hx5d, hx4), hx4), dim=1))
        hx3d = self.dec3(torch.cat((upsample(hx4d, hx3), hx3), dim=1))
        hx2d = self.dec2(torch.cat((upsample(hx3d, hx2), hx2), dim=1))
        hx1d = self.dec1(torch.cat((upsample(hx2d, hx1), hx1), dim=1))

        # === Side Outputs ===
        sup1 = self.side1(hx1d)
        sup2 = upsample(self.side2(hx2d), sup1)
        sup3 = upsample(self.side3(hx3d), sup1)
        sup4 = upsample(self.side4(hx4d), sup1)
        sup5 = upsample(self.side5(hx5d), sup1)
        sup6 = upsample(self.side6(hx6), sup1)

        # === Fusion ===
        sup0 = self.outconv(torch.cat((sup6, sup5, sup4, sup3, sup2, sup1), dim=1))

        # Logits as outputs for deep supervision using BCEWithLogitsLoss (numerically stable)
        return sup0, sup1, sup2, sup3, sup4, sup5, sup6 
    
# model = U2Net().cuda()
# in_ = torch.rand(size=(5,3,320,320)).to("cuda")
# print(model(in_))