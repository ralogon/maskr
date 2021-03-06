from fastai import *
from maskr import loss
from maskr.test.baseline import save
import logging
log = logging.getLogger()

class Multiloss(LearnerCallback):
    """ calculate multiple loss functions, sum, and save results """

    def on_train_begin(self, **kwargs:Any):
        self.losses = []

    def on_loss_begin(self, **kwargs):
        config = self.learn.model.config

        # get inputs
        tgt_rpn_match, tgt_rpn_bbox,\
        rpn_class_logits, rpn_bbox,\
        target_class_ids, target_deltas, target_mask,\
        mrcnn_class_logits, mrcnn_bbox, mrcnn_mask = kwargs["last_output"]["out"]

        # rpn loss
        rpn_class_loss = loss.rpn_class(tgt_rpn_match, rpn_class_logits)
        rpn_bbox_loss = loss.rpn_bbox(tgt_rpn_bbox, tgt_rpn_match, rpn_bbox)
        losses = [rpn_class_loss, rpn_bbox_loss]

        # head loss
        if config.HEAD:
            mrcnn_class_loss = loss.mrcnn_class(target_class_ids, mrcnn_class_logits)
            mrcnn_bbox_loss = loss.mrcnn_bbox(target_deltas, target_class_ids, mrcnn_bbox)
            mrcnn_mask_loss = loss.mrcnn_mask(target_mask, target_class_ids, mrcnn_mask)
            losses.extend([mrcnn_class_loss, mrcnn_bbox_loss, mrcnn_mask_loss])

        # get the mean loss for each variable
        mean_losses = []
        for lossvar in losses:
            # No objects on image returns None loss. Perfect match returns 0 loss.
            # Remove the None and unsqueeze to concatenate
            losses = [itemloss.unsqueeze(0) for itemloss in lossvar if itemloss is not None]
            if losses:
                mean_loss = torch.cat(losses).mean()
                mean_losses.append(mean_loss)
            else:
                # this only happens if all the images in a batch have no targets.
                log.error("loss calculation failed for whole batch")
                mean_losses.append(torch.tensor(0.))

        # calculate total and output
        mean_losses = [loss.unsqueeze(0) for loss in mean_losses]
        total = torch.cat(mean_losses).sum()
        mean_losses = [total] + mean_losses
        log.info([f"{x}={loss.item():0.4f}" for x, loss in zip(["tot", "rc", "rb", "c", "b", "m"],mean_losses)])
        self.losses.append(mean_losses)

        return total

class Cuda(LearnerCallback):
    " sets config device during training/validation. Resets to CPU for dataloader as multiprocessing hangs cuda "

    def on_train_begin(self, **kwargs:Any):
        # use cpu for train dataloader
        torch.set_default_tensor_type(torch.FloatTensor)

    def on_batch_begin(self, **kwargs:Any):
        # use cuda after dataloader initialised
        if self.learn.model.config.DEVICE=="cuda":
            torch.set_default_tensor_type(torch.cuda.FloatTensor)

    def on_batch_end(self, **kwargs:Any):
        # use cpu for valid dataloader
        torch.set_default_tensor_type(torch.FloatTensor)

###### debugging ##############################################################

class TrainSave(LearnerCallback):
    """ save data during weight update """
    def on_batch_begin(self, **kwargs):
        xb = kwargs["last_input"]
        images, image_metas, tgt_rpn_match, tgt_rpn_bbox, gt_class_ids, gt_boxes, gt_masks = xb
        save(images, "images")
        save(gt_class_ids, "gt_class_ids")
        save(gt_boxes, "gt_boxes")
        ### UNSQUEEZE FOR COMPARISON WITH MASKMM0 HAS STRANGE LAST DIMENSION THAT IS NEVER USED?
        save(tgt_rpn_match.unsqueeze(-1), "rpn_match")
        save(tgt_rpn_bbox, "rpn_bbox")

    def on_backward_end(self, **kwargs:Any):
        """ save weights and gradients before step """
        for name, param in self.learn.model.named_parameters():
            if param.requires_grad:
                save(param, "back_" + name)
        for name, param in self.learn.model.named_parameters():
            if param.requires_grad:
                save(param.grad, "grad_"+name)

    def on_step_end(self, **kwargs:Any):
        """ save weights after step """
        for name, param in self.learn.model.named_parameters():
            if param.requires_grad:
                save(param, "step_"+name)

class StrictBnFreeze(LearnerCallback):
    """ set all batchnorm to eval as original maskr
    fastai Bnfreeze only does this if next layer has requires_grad=False
    """
    def on_epoch_begin(self, **kwargs:Any):
        def set_bn_eval(m):
            classname = m.__class__.__name__
            if classname.find('BatchNorm') != -1:
                m.eval()
        self.learn.model.apply(set_bn_eval)