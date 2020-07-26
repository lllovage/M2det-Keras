from keras.applications.imagenet_utils import preprocess_input
from keras import backend as K
import keras
import tensorflow as tf
import numpy as np
from random import shuffle
from utils import backend
from PIL import Image
from keras.utils.data_utils import get_file
from matplotlib.colors import rgb_to_hsv, hsv_to_rgb
import cv2

def rand(a=0, b=1):
    return np.random.rand()*(b-a) + a
    
def softmax_loss(y_true, y_pred):
    y_pred = tf.maximum(y_pred, 1e-7)
    softmax_loss = -tf.reduce_sum(y_true * tf.log(y_pred),
                                    axis=-1)
    return softmax_loss

def conf_loss(neg_pos_ratio = 3,negatives_for_hard = 100):
    def _conf_loss(y_true, y_pred):
        batch_size = tf.shape(y_true)[0]
        num_boxes = tf.to_float(tf.shape(y_true)[1])
        
        labels         = y_true[:, :, :-1]
        classification = y_pred

        cls_loss = softmax_loss(labels, classification)
        
        num_pos = tf.reduce_sum(y_true[:, :, -1], axis=-1)
        
        pos_conf_loss = tf.reduce_sum(cls_loss * y_true[:, :, -1],
                                      axis=1)
        # 获取一定的负样本
        num_neg = tf.minimum(neg_pos_ratio * num_pos,
                             num_boxes - num_pos)


        # 找到了哪些值是大于0的
        pos_num_neg_mask = tf.greater(num_neg, 0)
        # 获得一个1.0
        has_min = tf.to_float(tf.reduce_any(pos_num_neg_mask))
        num_neg = tf.concat( axis=0,values=[num_neg,
                                [(1 - has_min) * negatives_for_hard]])

        # 求平均每个图片要取多少个负样本
        num_neg_batch = tf.reduce_mean(tf.boolean_mask(num_neg,
                                                      tf.greater(num_neg, 0)))
        num_neg_batch = tf.to_int32(num_neg_batch)

        max_confs = tf.reduce_max(y_pred[:, :, 1:],
                                  axis=2)

        # 取top_k个置信度，作为负样本
        x, indices = tf.nn.top_k(max_confs * (1 - y_true[:, :, -1]),
                                 k=num_neg_batch)

        # 找到其在1维上的索引
        batch_idx = tf.expand_dims(tf.range(0, batch_size), 1)
        batch_idx = tf.tile(batch_idx, (1, num_neg_batch))
        full_indices = (tf.reshape(batch_idx, [-1]) * tf.to_int32(num_boxes) +
                        tf.reshape(indices, [-1]))

        neg_conf_loss = tf.gather(tf.reshape(cls_loss, [-1]),
                                  full_indices)
        neg_conf_loss = tf.reshape(neg_conf_loss,
                                   [batch_size, num_neg_batch])
        neg_conf_loss = tf.reduce_sum(neg_conf_loss, axis=1)


        num_pos = tf.where(tf.not_equal(num_pos, 0), num_pos,
                            tf.ones_like(num_pos))
        total_loss = tf.reduce_sum(pos_conf_loss) + tf.reduce_sum(neg_conf_loss)
        total_loss /= tf.reduce_sum(num_pos)
        # total_loss = tf.Print(total_loss,[labels,full_indices,tf.reduce_sum(pos_conf_loss)/tf.reduce_sum(num_pos),tf.reduce_sum(neg_conf_loss)/tf.reduce_sum(num_pos),tf.reduce_sum(num_pos)])
        return total_loss
    return _conf_loss


def smooth_l1(sigma=1.0):
    sigma_squared = sigma ** 2

    def _smooth_l1(y_true, y_pred):
        # y_true [batch_size, num_anchor, 4+1]
        # y_pred [batch_size, num_anchor, 4]
        regression        = y_pred
        regression_target = y_true[:, :, :-1]
        anchor_state      = y_true[:, :, -1]

        # 找到正样本
        indices           = tf.where(keras.backend.equal(anchor_state, 1))
        regression        = tf.gather_nd(regression, indices)
        regression_target = tf.gather_nd(regression_target, indices)

        # 计算 smooth L1 loss
        # f(x) = 0.5 * (sigma * x)^2          if |x| < 1 / sigma / sigma
        #        |x| - 0.5 / sigma / sigma    otherwise
        regression_diff = regression - regression_target
        regression_diff = keras.backend.abs(regression_diff)
        regression_loss = backend.where(
            keras.backend.less(regression_diff, 1.0 / sigma_squared),
            0.5 * sigma_squared * keras.backend.pow(regression_diff, 2),
            regression_diff - 0.5 / sigma_squared
        )

        normalizer = keras.backend.maximum(1, keras.backend.shape(indices)[0])
        normalizer = keras.backend.cast(normalizer, dtype=keras.backend.floatx())
        loss = keras.backend.sum(regression_loss) / normalizer

        return loss

    return _smooth_l1

class Generator(object):
    def __init__(self, bbox_util,batch_size,
                 train_lines, val_lines, image_size,num_classes,
                 ):
        self.bbox_util = bbox_util
        self.batch_size = batch_size
        self.train_lines = train_lines
        self.val_lines = val_lines
        self.train_batches = len(train_lines)
        self.val_batches = len(val_lines)
        self.image_size = image_size
        self.num_classes = num_classes-1
        
    def get_random_data(self, annotation_line, input_shape, jitter=.3, hue=.1, sat=1.5, val=1.5):
        '''r实时数据增强的随机预处理'''
        line = annotation_line.split()
        image = Image.open(line[0])
        iw, ih = image.size
        h, w = input_shape
        box = np.array([np.array(list(map(int,box.split(',')))) for box in line[1:]])

        # resize image
        new_ar = w/h * rand(1-jitter,1+jitter)/rand(1-jitter,1+jitter)
        scale = rand(.25, 2)
        if new_ar < 1:
            nh = int(scale*h)
            nw = int(nh*new_ar)
        else:
            nw = int(scale*w)
            nh = int(nw/new_ar)
        image = image.resize((nw,nh), Image.BICUBIC)

        # place image
        dx = int(rand(0, w-nw))
        dy = int(rand(0, h-nh))
        new_image = Image.new('RGB', (w,h), (128,128,128))
        new_image.paste(image, (dx, dy))
        image = new_image

        # flip image or not
        flip = rand()<.5
        if flip: image = image.transpose(Image.FLIP_LEFT_RIGHT)

        # distort image
        hue = rand(-hue, hue)
        sat = rand(1, sat) if rand()<.5 else 1/rand(1, sat)
        val = rand(1, val) if rand()<.5 else 1/rand(1, val)
        x = cv2.cvtColor(np.array(image,np.float32)/255, cv2.COLOR_RGB2HSV)
        x[..., 0] += hue*360
        x[..., 0][x[..., 0]>1] -= 1
        x[..., 0][x[..., 0]<0] += 1
        x[..., 1] *= sat
        x[..., 2] *= val
        x[x[:,:, 0]>360, 0] = 360
        x[:, :, 1:][x[:, :, 1:]>1] = 1
        x[x<0] = 0
        image_data = cv2.cvtColor(x, cv2.COLOR_HSV2RGB)*255

        # correct boxes
        box_data = np.zeros((len(box),5))
        if len(box)>0:
            np.random.shuffle(box)
            box[:, [0,2]] = box[:, [0,2]]*nw/iw + dx
            box[:, [1,3]] = box[:, [1,3]]*nh/ih + dy
            if flip: box[:, [0,2]] = w - box[:, [2,0]]
            box[:, 0:2][box[:, 0:2]<0] = 0
            box[:, 2][box[:, 2]>w] = w
            box[:, 3][box[:, 3]>h] = h
            box_w = box[:, 2] - box[:, 0]
            box_h = box[:, 3] - box[:, 1]
            box = box[np.logical_and(box_w>1, box_h>1)] # discard invalid box
            box_data = np.zeros((len(box),5))
            box_data[:len(box)] = box
        if len(box) == 0:
            return image_data, []

        if (box_data[:,:4]>0).any():
            return image_data, box_data
        else:
            return image_data, []

    
    def generate(self, train=True):
        while True:
            if train:
                # 打乱
                shuffle(self.train_lines)
                lines = self.train_lines
            else:
                shuffle(self.val_lines)
                lines = self.val_lines
            inputs = []
            target0 = []
            target1 = []
            for annotation_line in lines:  
                img,y=self.get_random_data(annotation_line,self.image_size[0:2])
                if len(y)!=0:
                    boxes = np.array(y[:,:4],dtype=np.float32)
                    boxes[:,0] = boxes[:,0]/self.image_size[1]
                    boxes[:,1] = boxes[:,1]/self.image_size[0]
                    boxes[:,2] = boxes[:,2]/self.image_size[1]
                    boxes[:,3] = boxes[:,3]/self.image_size[0]
                    one_hot_label = np.eye(self.num_classes)[np.array(y[:,4],np.int32)]
                    
                    y = np.concatenate([boxes,one_hot_label],axis=-1)
                # print(y)
                # 计算真实框对应的先验框，与这个先验框应当有的预测结果
                assignment = self.bbox_util.assign_boxes(y)
                regression = assignment[:,:5]
                classification = assignment[:,5:]
                inputs.append(img)          
                target0.append(np.reshape(regression,[-1,5]))
                target1.append(np.reshape(classification,[-1,self.num_classes+2]))
                if len(target0) == self.batch_size:
                    tmp_inp = np.array(inputs)
                    tmp_targets = [np.array(target0,dtype=np.float32),np.array(target1,dtype=np.float32)]
                    inputs = []
                    target0 = []
                    target1 = []
                    yield preprocess_input(tmp_inp), tmp_targets
