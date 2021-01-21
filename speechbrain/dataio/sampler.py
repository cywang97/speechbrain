"""PyTorch compatible samplers

These determine the order of iteration through a dataset.

Authors:
  * Aku Rouhe 2020
"""
import torch
import logging
from operator import itemgetter
from torch.utils.data import RandomSampler, DistributedSampler, Sampler
import numpy as np

logger = logging.getLogger(__name__)


class ReproducibleRandomSampler(RandomSampler):
    """
    A modification of RandomSampler which always returns the same values.

    Also look at `torch.utils.data.RandomSampler`. This has mostly
    the same behaviour and arguments, except for adding 'seed' and 'epoch' and
    not supporting 'generator'.

    Note
    ----
    Call `set_epoch` before every epoch. Otherwise, the sampler will produce the
    same sequence of indices every epoch.

    Arguments
    ---------
    seed : int
        The base seed to use for the random number generator. It is recommended
        to use a value which has a good mix of 0 and 1 bits.
    epoch : int
        The epoch to start at.

    Example
    -------
    >>> import torch
    >>> from speechbrain.utils.checkpoints import Checkpointer
    >>> from speechbrain.dataio.dataloader import SaveableDataLoader
    >>> # An example "dataset"
    >>> dataset = torch.arange(10).unsqueeze(1)
    >>> # Create the random sampler:
    >>> sampler = ReproducibleRandomSampler(dataset)
    >>> dataloader = SaveableDataLoader(dataset, sampler = sampler,
    ...     num_workers = 3)
    >>> # Setup the checkpointer.
    >>> # Note that the sampler doesn't need to be saved itself.
    >>> tmpdir = getfixture('tmpdir')
    >>> checkpointer = Checkpointer(tmpdir, {"dataloader": dataloader})
    >>> # Iterate:
    >>> subset = []
    >>> for i, data_point in enumerate(dataloader):
    ...     # Say you save a checkpoint on the fourth batch:
    ...     if i == 3:
    ...         _ = checkpointer.save_checkpoint(end_of_epoch = False)
    ...     # So let's save the numbers you would get if you continue
    ...     if i >= 4:
    ...         subset.append(data_point.item())
    >>> # What if instead you had to restart the experiment?
    >>> new_sampler = ReproducibleRandomSampler(dataset)
    >>> new_dataloader = SaveableDataLoader(dataset, sampler = new_sampler,
    ...        num_workers = 3)
    >>> new_checkpointer = Checkpointer(tmpdir, {"dataloader": new_dataloader})
    >>> _ = new_checkpointer.recover_if_possible()
    >>> # You'll get the same random order again:
    >>> new_subset = [data_point.item() for data_point in new_dataloader]
    >>> assert subset == new_subset

    """

    def __init__(self, data_source, seed=563375142, epoch=0, **kwargs):
        if "generator" in kwargs:
            MSG = (
                "Cannot give a separate generator when using "
                + "ReproducibleRandomSampler"
            )
            raise ValueError(MSG)
        super().__init__(data_source, **kwargs)
        self.seed = int(seed)
        self.epoch = epoch
        self.generator = torch.Generator()

    def set_epoch(self, epoch):
        """
        You can also just access self.epoch, but we maintain this interface
        to mirror torch.utils.data.distributed.DistributedSampler
        """
        self.epoch = epoch

    def __iter__(self):
        self.generator.manual_seed(self.seed + self.epoch)
        return super().__iter__()


class ConcatDatasetBatchSampler(Sampler):
    def __init__(self, samplers, batch_sizes: (tuple, list), epoch=0) -> None:

        if not isinstance(samplers, (list, tuple)):
            raise ValueError(
                "samplers should be a list or tuple of Pytorch Samplers, "
                "but got samplers={}".format(batch_sizes)
            )

        if not isinstance(batch_sizes, (list, tuple)):
            raise ValueError(
                "batch_sizes should be a list or tuple of integers, "
                "but got batch_sizes={}".format(batch_sizes)
            )

        if not len(batch_sizes) == len(samplers):
            raise ValueError(
                "batch_sizes and samplers should be have same length"
            )

        self.batch_sizes = batch_sizes
        self.samplers = samplers
        self.offsets = [0] + np.cumsum(
            [len(x) for x in self.samplers]
        ).tolist()[:-1]

        self.epoch = epoch
        self.set_epoch(self.epoch)

    def _iter_one_dataset(self, c_batch_size, c_sampler, c_offset):
        batch = []
        for idx in c_sampler:
            batch.append(c_offset + idx)
            if len(batch) == c_batch_size:
                yield batch

    def set_epoch(self, epoch):
        """
        You can also just access self.epoch, but we maintain this interface
        to mirror torch.utils.data.distributed.DistributedSampler
        """
        if hasattr(self.samplers[0], "epoch"):
            for s in self.samplers:
                s.set_epoch(epoch)

    def __iter__(self):

        iterators = [iter(i) for i in self.samplers]
        tot_batch = []

        for b_num in range(len(self)):
            for samp_idx in range(len(self.samplers)):
                c_batch = []
                while len(c_batch) < self.batch_sizes[samp_idx]:
                    c_batch.append(
                        self.offsets[samp_idx] + next(iterators[samp_idx])
                    )
                tot_batch.extend(c_batch)
            yield tot_batch
            tot_batch = []

    def __len__(self):

        min_len = float("inf")
        for idx, sampler in enumerate(self.samplers):
            c_len = (
                len(sampler) + self.batch_sizes[idx] - 1
            ) // self.batch_sizes[idx]

            min_len = min(c_len, min_len)
        return min_len


# Heavily inspired by Catalyst, which is under Apache 2.0 licence.
# https://github.com/catalyst-team/catalyst/blob/51428d7756e62b9b8ee5379f38e9fd576eeb36e5/catalyst/data/sampler.py#L522
class DistributedSamplerWrapper(DistributedSampler):
    """Allows using any sampler with Distributed Data Parallel"""

    def __init__(self, sampler, *args, **kwargs):
        # DistributedSampler only calls len() on dataset
        # so a sampler is fine to pass there, as well.
        super().__init__(dataset=sampler, *args, **kwargs)
        self.sampler = sampler

    def __iter__(self):
        # It is easiest to use a random access interface to the wrapped
        # sampler's indices, so we just fetch all indices from the wrapped
        # sampler
        sampler_indices = list(self.sampler.__iter__())
        indices_of_indices = super().__iter__()
        # Itemgetter fetches the wrapped sampler indices from the positions
        # pointed to by DistributedSampler
        return iter(itemgetter(*indices_of_indices)(sampler_indices))

    def set_epoch(self, epoch):
        """Pass set_epoch() through to DistributedSampler and the wrapper one"""
        super().set_epoch(epoch)
        if hasattr(self.sampler, "set_epoch"):
            self.sampler.set_epoch(epoch)