# Copyright 2017-2021 Lawrence Livermore National Security, LLC and other
# Hatchet Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: MIT

import pandas as pd

from collections import OrderedDict
import os
import sys
import json

# Make it work for Python 2+3 and with Unicode
try:
    to_unicode = unicode
except NameError:
    to_unicode = str

import hatchet.graphframe
from hatchet.node import Node
from hatchet.graph import Graph
from hatchet.frame import Frame


class PAPIReader:
    def __init__(self, file_path, filter):
        # this is the name of the PAPI performance report directory. The directory
        # contains json files per MPI rank.

        self.inc_metrics = []
        self.dict = {}
        self.filter = filter
        # used to keep track of region ids in graph
        self.node_graph_dict = OrderedDict()

        json_dict = {}
        json_rank = OrderedDict()

        # check whether the file path is a file or a directory
        if os.path.isdir(file_path):

            # get measurement files
            file_list = sorted(os.listdir(file_path))
            rank_cnt = 0

            for item in file_list:
                # determine mpi rank based on file name (rank_#)
                try:
                    rank = item.split("_", 1)[1]
                    rank = rank.rsplit(".", 1)[0]
                except BaseException:
                    # skip file
                    print(
                        "Warning: {} has the wrong format. It will be skipped.".format(
                            item
                        )
                    )
                    continue

                # we use the rank counter starting from 0
                rank = rank_cnt

                # open measurement file
                file_name = str(file_path) + "/" + str(item)

                try:
                    with open(file_name) as json_file:
                        # keep order of all objects
                        try:
                            data = json.load(json_file, object_pairs_hook=OrderedDict)
                        except BaseException:
                            print("Error: {} is not json.".format(file_path))
                            sys.exit()
                except IOError as ioe:
                    print(
                        "Error: Cannot open file {} ({})".format(file_name, repr(ioe))
                    )
                    sys.exit()

                # get all events
                if not self.inc_metrics:
                    self.inc_metrics = list(data["event_definitions"].keys())

                # get all threads
                json_rank[str(rank)] = OrderedDict()
                json_rank[str(rank)]["threads"] = data["threads"]

                rank_cnt = rank_cnt + 1

            json_dict["ranks"] = json_rank
            self.dict = json_dict

        elif os.path.isfile(file_path):

            try:
                with open(file_path) as json_file:
                    # keep order of all objects
                    try:
                        data = json.load(json_file, object_pairs_hook=OrderedDict)
                    except BaseException:
                        print("Error: {} is not json.".format(file_path))
                        sys.exit()
            except IOError as ioe:
                print("Error: Cannot open file {} ({})".format(file_name, repr(ioe)))
                sys.exit()

            # get all events
            if not self.inc_metrics:
                self.inc_metrics = list(data["event_definitions"].keys())

            json_rank["0"] = OrderedDict()
            json_rank["0"]["threads"] = data["threads"]
            json_dict["ranks"] = json_rank
            self.dict = json_dict

        else:
            print("Error: {} does not exist.".format(file_path))
            sys.exit()

    def __find_read_node(self, parent_node, name):
        for node in list(parent_node.traverse()):
            if node.frame.values("name") == name:
                return node
                break
        return None

    def __add_child_node(self, id, child_node):
        parent_node = self.node_graph_dict[id][1]
        child_node.add_parent(parent_node)
        parent_node.add_child(child_node)

    def __get_metrics(self, data, contain_read_events):
        metrics = OrderedDict()
        for metric in self.inc_metrics:
            if isinstance(data[metric], dict):
                contain_read_events[0] = 1
                metrics[metric] = int(data[metric]["region_value"])
            else:
                metrics[metric] = int(data[metric])
        return metrics

    def __get_read_metrics(self, data, read_id):
        metrics = OrderedDict()
        for metric in self.inc_metrics:
            if isinstance(data[metric], dict):
                metrics[metric] = int(data[metric][read_id])
        return metrics

    def __get_zero_metrics(self):
        metrics = OrderedDict()
        for metric in self.inc_metrics:
            metrics[metric] = 0
        return metrics

    def __create_graph(self, rank, thread, list_roots, node_dicts):
        graph_data = self.dict["ranks"][str(rank)]["threads"][str(thread)]["regions"]
        for region, data in iter(graph_data.items()):
            # print(region, data)

            frame = Frame({"type": "region", "name": data["name"]})
            node = Node(frame, None)

            contain_read_events = [0]
            metrics = self.__get_metrics(data, contain_read_events)

            node_dict = dict(
                {
                    "name": data["name"],
                    "node": node,
                    "rank": int(rank),
                    "thread": int(thread),
                    **metrics,
                }
            )
            node_dicts.append(node_dict)

            # used to find node using parent_region_id
            self.node_graph_dict[int(region)] = [data["name"], node]

            if int(data["parent_region_id"]) == -1:
                list_roots.append(node)
            else:
                self.__add_child_node(int(data["parent_region_id"]), node)

            # check if we have to create child nodes for read events
            if contain_read_events[0] == 1:

                # check how many read calls are used
                read_num = len(data["cycles"])

                for i in range(1, read_num):
                    node_name_read = "read_" + str(i)

                    read_frame = Frame(
                        {"type": "region", "name": node_name_read}
                    )
                    read_node = Node(read_frame, node)
                    read_metrics = self.__get_read_metrics(data, node_name_read)
                    node_dict = dict(
                        {
                            "name": node_name_read,
                            "node": read_node,
                            "rank": int(rank),
                            "thread": int(thread),
                            **read_metrics,
                        }
                    )
                    node_dicts.append(node_dict)
                    node.add_child(read_node)


    def __print_error_and_exit(self, region_name, rank, thread):
        print(
            'Error: Cannot assign region "{}" (rank={}, thread={}) to the Hatchet graph due to non matching subgraphs of different threads.\nPlease read in only one specific rank file and make sure all threads have the same instrumented regions.'.format(
                region_name, rank, thread
            )
        )
        exit(1)

    def read(self):
        # print(self.dict)

        # filter regions
        if len(self.filter) > 0:
            for rank, rank_value in list(self.dict["ranks"].items()):
                for thread, thread_value in list(rank_value["threads"].items()):
                    for region, data in list(thread_value["regions"].items()):
                        if any(map(data["name"].__contains__, self.filter)) is True:
                            del self.dict["ranks"][str(rank)]["threads"][str(thread)][
                                "regions"
                            ][str(region)]

        # add default metrics 'cycles' and 'real_time_nsec' to inc_metrics
        self.inc_metrics.append("cycles")
        self.inc_metrics.append("real_time_nsec")

        # determine thread with the largest number of regions to create the graph
        max_regions = 1
        graph_rank = 0
        graph_thread = 0

        rank_cnt = 0
        thread_cnt = 0
        for rank, rank_value in iter(self.dict["ranks"].items()):
            rank_cnt += 1
            for thread, thread_value in iter(rank_value["threads"].items()):
                thread_cnt += 1
                if len(thread_value["regions"]) > max_regions:
                    max_regions = len(thread_value["regions"])
                    graph_rank = int(rank)
                    graph_thread = int(thread)

        # create graph
        list_roots = []
        node_dicts = []
        self.__create_graph(
            graph_rank, graph_thread, list_roots, node_dicts
        )

        # fill up node dictionaries for all remaining ranks and threads
        for rank, rank_value in iter(self.dict["ranks"].items()):
            for thread, thread_value in iter(rank_value["threads"].items()):
                if int(rank) != graph_rank or int(thread) != graph_thread:
                    node_graph_id = -1
                    for data in iter(thread_value["regions"].values()):
                        # print(data["name"])

                        node_graph_id += 1
                        if node_graph_id >= len(self.node_graph_dict):
                            self.__print_error_and_exit(data["name"], rank, thread)

                        # find matching regions
                        found_match = False
                        while found_match is False:
                            if self.node_graph_dict[node_graph_id][0] == data["name"]:
                                found_match = True
                            else:
                                # create a tuple of zero values
                                zero_metrics = self.__get_zero_metrics()
                                node_dict = dict(
                                    {
                                        "name": self.node_graph_dict[node_graph_id][0],
                                        "node": self.node_graph_dict[node_graph_id][1],
                                        "rank": int(rank),
                                        "thread": int(thread),
                                        **zero_metrics,
                                    }
                                )
                                node_dicts.append(node_dict)

                                # set index to the next region
                                node_graph_id += 1
                                if node_graph_id >= len(self.node_graph_dict):
                                    self.__print_error_and_exit(data["name"], rank, thread)

                        if found_match is True:
                            # we found a match
                            contain_read_events = [0]
                            metrics = self.__get_metrics(data, contain_read_events)

                            node_dict = dict(
                                {
                                    "name": self.node_graph_dict[node_graph_id][0],
                                    "node": self.node_graph_dict[node_graph_id][1],
                                    "rank": int(rank),
                                    "thread": int(thread),
                                    **metrics,
                                }
                            )
                            node_dicts.append(node_dict)
                            # check if we have to add read events
                            if contain_read_events[0] == 1:

                                # check how many read calls are used
                                read_num = len(data["cycles"])

                                for i in range(1, read_num):
                                    node_name_read = "read_" + str(i)

                                    read_metrics = self.__get_read_metrics(
                                        data, node_name_read
                                    )
                                    node_dict = dict(
                                        {
                                            "name": node_name_read,
                                            "node": self.__find_read_node(
                                                self.node_graph_dict[node_graph_id][1],
                                                node_name_read,
                                            ),
                                            "rank": int(rank),
                                            "thread": int(thread),
                                            **read_metrics,
                                        }
                                    )
                                    node_dicts.append(node_dict)

        # setup data for hatchet graphframe
        graph = Graph(list_roots)
        graph.enumerate_traverse()

        dataframe = pd.DataFrame(data=node_dicts)

        # check graph indices
        if rank_cnt > 1 and thread_cnt > 1:
            indices = ["node", "rank", "thread"]
        elif rank_cnt > 1:
            dataframe.drop(columns=["thread"], inplace=True)
            indices = ["node", "rank"]
        elif thread_cnt > 1:
            dataframe.drop(columns=["rank"], inplace=True)
            indices = ["node", "thread"]
        else:
            dataframe.drop(columns=["rank", "thread"], inplace=True)
            indices = ["node"]

        dataframe.set_index(indices, inplace=True)
        dataframe.sort_index(inplace=True)

        default_metric = "real_time_nsec"

        return hatchet.graphframe.GraphFrame(graph, dataframe, [], self.inc_metrics, default_metric)
