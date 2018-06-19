import numpy as np
import pyopencl as cl
from pyopencl.tools import dtype_to_ctype
from mako.template import Template

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner \
                 Copyright (C) 2018 Hao Gao"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


def partition_work(traversal, total_rank, workload_weight):
    """ This function assigns responsible boxes of each process.

    Each process is responsible for calculating the multiple expansions as well as
    evaluating target potentials in *responsible_boxes*.

    :arg traversal: The traversal object built on root containing all particles.
    :arg total_rank: The total number of processes.
    :arg workload_weight: Workload coefficients of various operations (e.g. direct
        evaluations, multipole-to-local, etc.) used for load balacing.
    :return: A numpy array of shape (total_rank,), where the ith element is an numpy
        array containing the responsible boxes of process i.
    """
    tree = traversal.tree

    # store the workload of each box
    workload = np.zeros((tree.nboxes,), dtype=np.float64)

    # add workload of list 1
    for itarget_box, box_idx in enumerate(traversal.target_boxes):
        box_ntargets = tree.box_target_counts_nonchild[box_idx]
        start = traversal.neighbor_source_boxes_starts[itarget_box]
        end = traversal.neighbor_source_boxes_starts[itarget_box + 1]
        list1 = traversal.neighbor_source_boxes_lists[start:end]
        particle_count = 0
        for ibox in list1:
            particle_count += tree.box_source_counts_nonchild[ibox]
        workload[box_idx] += box_ntargets * particle_count * workload_weight.direct

    # add workload of list 2
    for itarget_or_target_parent_boxes, box_idx in enumerate(
            traversal.target_or_target_parent_boxes):
        start = traversal.from_sep_siblings_starts[itarget_or_target_parent_boxes]
        end = traversal.from_sep_siblings_starts[itarget_or_target_parent_boxes + 1]
        workload[box_idx] += (end - start) * workload_weight.m2l

    for ilevel in range(tree.nlevels):
        # add workload of list 3 far
        for itarget_box, box_idx in enumerate(
                traversal.target_boxes_sep_smaller_by_source_level[ilevel]):
            box_ntargets = tree.box_target_counts_nonchild[box_idx]
            start = traversal.from_sep_smaller_by_level[ilevel].starts[itarget_box]
            end = traversal.from_sep_smaller_by_level[ilevel].starts[
                                                                itarget_box + 1]
            workload[box_idx] += (end - start) * box_ntargets

        # add workload of list 3 near
        if tree.targets_have_extent and \
                traversal.from_sep_close_smaller_starts is not None:
            for itarget_box, box_idx in enumerate(traversal.target_boxes):
                box_ntargets = tree.box_target_counts_nonchild[box_idx]
                start = traversal.from_sep_close_smaller_starts[itarget_box]
                end = traversal.from_sep_close_smaller_starts[itarget_box + 1]
                particle_count = 0
                for near_box_id in traversal.from_sep_close_smaller_lists[start:end]:
                    particle_count += tree.box_source_counts_nonchild[near_box_id]
                workload[box_idx] += (
                    box_ntargets * particle_count * workload_weight.direct)

    # add workload of list 4
    for itarget_or_target_parent_boxes, box_idx in enumerate(
            traversal.target_or_target_parent_boxes):
        start = traversal.from_sep_bigger_starts[itarget_or_target_parent_boxes]
        end = traversal.from_sep_bigger_starts[itarget_or_target_parent_boxes + 1]
        particle_count = 0
        for far_box_id in traversal.from_sep_bigger_lists[start:end]:
            particle_count += tree.box_source_counts_nonchild[far_box_id]
        workload[box_idx] += particle_count * workload_weight.p2l

        if tree.targets_have_extent and \
                traversal.from_sep_close_bigger_starts is not None:
            box_ntargets = tree.box_target_counts_nonchild[box_idx]
            start = traversal.from_sep_close_bigger_starts[
                        itarget_or_target_parent_boxes]
            end = traversal.from_sep_close_bigger_starts[
                        itarget_or_target_parent_boxes + 1]
            particle_count = 0
            for direct_box_id in traversal.from_sep_close_bigger_lists[start:end]:
                particle_count += tree.box_source_counts_nonchild[direct_box_id]
            workload[box_idx] += (
                    box_ntargets * particle_count * workload_weight.direct)

    for i in range(tree.nboxes):
        # add workload of multipole calculation
        workload[i] += tree.box_source_counts_nonchild[i] * workload_weight.multipole

    total_workload = 0
    for i in range(tree.nboxes):
        total_workload += workload[i]

    # transform tree from level order to dfs order
    dfs_order = np.empty((tree.nboxes,), dtype=tree.box_id_dtype)
    idx = 0
    stack = [0]
    while len(stack) > 0:
        box_id = stack.pop()
        dfs_order[idx] = box_id
        idx += 1
        for i in range(2**tree.dimensions):
            child_box_id = tree.box_child_ids[i][box_id]
            if child_box_id > 0:
                stack.append(child_box_id)

    # partition all boxes in dfs order evenly according to workload
    responsible_boxes_list = np.empty((total_rank,), dtype=object)
    rank = 0
    start = 0
    workload_count = 0
    for i in range(tree.nboxes):
        box_idx = dfs_order[i]
        workload_count += workload[box_idx]
        if (workload_count > (rank + 1)*total_workload/total_rank or
                i == tree.nboxes - 1):
            responsible_boxes_list[rank] = dfs_order[start:i+1]
            start = i + 1
            rank += 1

    return responsible_boxes_list


class ResponsibleBoxesQuery(object):
    """ Query related to the responsible boxes for a given traversal.
    """

    def __init__(self, queue, traversal):
        """
        :param queue: A pyopencl.CommandQueue object.
        :param traversal: The global traversal built on root with all particles.
        """
        self.queue = queue
        self.traversal = traversal
        self.tree = traversal.tree

        # {{{ fetch tree structure and interaction lists to device memory

        self.box_parent_ids_dev = cl.array.to_device(queue, self.tree.box_parent_ids)
        self.target_boxes_dev = cl.array.to_device(queue, traversal.target_boxes)
        self.target_or_target_parent_boxes_dev = cl.array.to_device(
            queue, traversal.target_or_target_parent_boxes)

        # list 1
        self.neighbor_source_boxes_starts_dev = cl.array.to_device(
            queue, traversal.neighbor_source_boxes_starts)
        self.neighbor_source_boxes_lists_dev = cl.array.to_device(
            queue, traversal.neighbor_source_boxes_lists)

        # list 2
        self.from_sep_siblings_starts_dev = cl.array.to_device(
            queue, traversal.from_sep_siblings_starts)
        self.from_sep_siblings_lists_dev = cl.array.to_device(
            queue, traversal.from_sep_siblings_lists)

        # list 3
        self.target_boxes_sep_smaller_by_source_level_dev = np.empty(
            (self.tree.nlevels,), dtype=object)
        for ilevel in range(self.tree.nlevels):
            self.target_boxes_sep_smaller_by_source_level_dev[ilevel] = \
                cl.array.to_device(
                    queue,
                    traversal.target_boxes_sep_smaller_by_source_level[ilevel]
                )

        self.from_sep_smaller_by_level_starts_dev = np.empty(
            (self.tree.nlevels,), dtype=object)
        for ilevel in range(self.tree.nlevels):
            self.from_sep_smaller_by_level_starts_dev[ilevel] = cl.array.to_device(
                queue, traversal.from_sep_smaller_by_level[ilevel].starts
            )

        self.from_sep_smaller_by_level_lists_dev = np.empty(
            (self.tree.nlevels,), dtype=object)
        for ilevel in range(self.tree.nlevels):
            self.from_sep_smaller_by_level_lists_dev[ilevel] = cl.array.to_device(
                queue, traversal.from_sep_smaller_by_level[ilevel].lists
            )

        # list 4
        self.from_sep_bigger_starts_dev = cl.array.to_device(
            queue, traversal.from_sep_bigger_starts)
        self.from_sep_bigger_lists_dev = cl.array.to_device(
            queue, traversal.from_sep_bigger_lists)

        # }}}

        if self.tree.targets_have_extent:
            # list 3 close
            if traversal.from_sep_close_smaller_starts is not None:
                self.from_sep_close_smaller_starts_dev = cl.array.to_device(
                    queue, traversal.from_sep_close_smaller_starts)
                self.from_sep_close_smaller_lists_dev = cl.array.to_device(
                    queue, traversal.from_sep_close_smaller_lists)

            # list 4 close
            if traversal.from_sep_close_bigger_starts is not None:
                self.from_sep_close_bigger_starts_dev = cl.array.to_device(
                    queue, traversal.from_sep_close_bigger_starts)
                self.from_sep_close_bigger_lists_dev = cl.array.to_device(
                    queue, traversal.from_sep_close_bigger_lists)

        # helper kernel for ancestor box query
        self.mark_parent_knl = cl.elementwise.ElementwiseKernel(
            queue.context,
            "__global char *current, __global char *parent, "
            "__global %s *box_parent_ids" % dtype_to_ctype(self.tree.box_id_dtype),
            "if(i != 0 && current[i]) parent[box_parent_ids[i]] = 1"
        )

        # helper kernel for adding boxes from interaction list 1 and 4
        self.add_interaction_list_boxes = cl.elementwise.ElementwiseKernel(
            queue.context,
            Template("""
                __global ${box_id_t} *box_list,
                __global char *responsible_boxes_mask,
                __global ${box_id_t} *interaction_boxes_starts,
                __global ${box_id_t} *interaction_boxes_lists,
                __global char *src_boxes_mask
            """, strict_undefined=True).render(
                box_id_t=dtype_to_ctype(self.tree.box_id_dtype)
            ),
            Template(r"""
                typedef ${box_id_t} box_id_t;
                box_id_t current_box = box_list[i];
                if(responsible_boxes_mask[current_box]) {
                    for(box_id_t box_idx = interaction_boxes_starts[i];
                        box_idx < interaction_boxes_starts[i + 1];
                        ++box_idx)
                        src_boxes_mask[interaction_boxes_lists[box_idx]] = 1;
                }
            """, strict_undefined=True).render(
                box_id_t=dtype_to_ctype(self.tree.box_id_dtype)
            ),
        )

    def ancestor_boxes_mask(self, responsible_boxes_mask):
        """ Query the ancestors of responsible boxes.

        :param responsible_boxes_mask: A pyopencl.array.Array object of shape
            (tree.nboxes,) whose ith entry is 1 iff i is a responsible box.
        :return: A pyopencl.array.Array object of shape (tree.nboxes,) whose ith
            entry is 1 iff i is either a responsible box or an ancestor of the
            responsible boxes specified by responsible_boxes_mask.
        """
        ancestor_boxes = cl.array.zeros(
            self.queue, (self.tree.nboxes,), dtype=np.int8)
        ancestor_boxes_last = responsible_boxes_mask.copy()

        while ancestor_boxes_last.any():
            ancestor_boxes_new = cl.array.zeros(self.queue, (self.tree.nboxes,),
                                                dtype=np.int8)
            self.mark_parent_knl(ancestor_boxes_last, ancestor_boxes_new,
                                 self.box_parent_ids_dev)
            ancestor_boxes_new = ancestor_boxes_new & (~ancestor_boxes)
            ancestor_boxes = ancestor_boxes | ancestor_boxes_new
            ancestor_boxes_last = ancestor_boxes_new

        return ancestor_boxes

    def src_boxes_mask(self, responsible_boxes_mask, ancestor_boxes_mask):
        """ Query the boxes whose sources are needed in order to evaluate potentials
        of boxes represented by responsible_boxes_mask.

        :param responsible_boxes_mask: A pyopencl.array.Array object of shape
            (tree.nboxes,) whose ith entry is 1 iff i is a responsible box.
        :param ancestor_boxes_mask: A pyopencl.array.Array object of shape
            (tree.nboxes,) whose ith entry is 1 iff i is either a responsible box
            or an ancestor of the responsible boxes.
        :return: A pyopencl.array.Array object of shape (tree.nboxes,) whose ith
            entry is 1 iff souces of box i are needed for evaluating the potentials
            of targets in boxes represented by responsible_boxes_mask.
        """
        src_boxes_mask = responsible_boxes_mask.copy()

        # Add list 1 of responsible boxes
        self.add_interaction_list_boxes(
            self.target_boxes_dev, responsible_boxes_mask,
            self.neighbor_source_boxes_starts_dev,
            self.neighbor_source_boxes_lists_dev, src_boxes_mask,
            range=range(0, self.traversal.target_boxes.shape[0])
        )

        # Add list 4 of responsible boxes or ancestor boxes
        self.add_interaction_list_boxes(
            self.target_or_target_parent_boxes_dev,
            responsible_boxes_mask | ancestor_boxes_mask,
            self.from_sep_bigger_starts_dev, self.from_sep_bigger_lists_dev,
            src_boxes_mask,
            range=range(0, self.traversal.target_or_target_parent_boxes.shape[0]))

        if self.tree.targets_have_extent:

            # Add list 3 close of responsible boxes
            if self.traversal.from_sep_close_smaller_starts is not None:
                self.add_interaction_list_boxes(
                    self.target_boxes_dev,
                    responsible_boxes_mask,
                    self.from_sep_close_smaller_starts_dev,
                    self.from_sep_close_smaller_lists_dev,
                    src_boxes_mask
                )

            # Add list 4 close of responsible boxes
            if self.traversal.from_sep_close_bigger_starts is not None:
                self.add_interaction_list_boxes(
                    self.target_or_target_parent_boxes_dev,
                    responsible_boxes_mask | ancestor_boxes_mask,
                    self.from_sep_close_bigger_starts_dev,
                    self.from_sep_close_bigger_lists_dev,
                    src_boxes_mask
                )

        return src_boxes_mask

    def multipole_boxes_mask(self, responsible_boxes_mask, ancestor_boxes_mask):
        """ Query the boxes whose multipoles are used in order to evaluate
        potentials of targets in boxes represented by responsible_boxes_mask.

        :param responsible_boxes_mask: A pyopencl.array.Array object of shape
            (tree.nboxes,) whose ith entry is 1 iff i is a responsible box.
        :param ancestor_boxes_mask: A pyopencl.array.Array object of shape
            (tree.nboxes,) whose ith entry is 1 iff i is either a responsible box
            or an ancestor of the responsible boxes.
        :return: A pyopencl.array.Array object of shape (tree.nboxes,) whose ith
            entry is 1 iff multipoles of box i are needed for evaluating the
            potentials of targets in boxes represented by responsible_boxes_mask.
        """

        multipole_boxes_mask = cl.array.zeros(self.queue, (self.tree.nboxes,),
                                              dtype=np.int8)

        # A mpole is used by process p if it is in the List 2 of either a box
        # owned by p or one of its ancestors.
        self.add_interaction_list_boxes(
            self.target_or_target_parent_boxes_dev,
            responsible_boxes_mask | ancestor_boxes_mask,
            self.from_sep_siblings_starts_dev,
            self.from_sep_siblings_lists_dev,
            multipole_boxes_mask
        )
        multipole_boxes_mask.finish()

        # A mpole is used by process p if it is in the List 3 of a box owned by p.
        for ilevel in range(self.tree.nlevels):
            self.add_interaction_list_boxes(
                self.target_boxes_sep_smaller_by_source_level_dev[ilevel],
                responsible_boxes_mask,
                self.from_sep_smaller_by_level_starts_dev[ilevel],
                self.from_sep_smaller_by_level_lists_dev[ilevel],
                multipole_boxes_mask
            )

            multipole_boxes_mask.finish()

        return multipole_boxes_mask
