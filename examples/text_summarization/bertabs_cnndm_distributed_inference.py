# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import argparse
import os
import sys
import time
import torch
import torch.distributed as dist
import torch.multiprocessing as mp

from torch.multiprocessing import Manager
from copy import deepcopy
#torch.set_printoptions(threshold=5000)
from tempfile import TemporaryDirectory

nlp_path = os.path.abspath("../../")
if nlp_path not in sys.path:
    sys.path.insert(0, nlp_path)


from utils_nlp.models.transformers.abssum import AbsSum, AbsSumProcessor, validate
from utils_nlp.models.transformers.bertabs import model_builder

from utils_nlp.dataset.cnndm import CNNDMBertSumProcessedData, CNNDMSummarizationDataset
from utils_nlp.models.transformers.datasets import SummarizationNonIterableDataset
from utils_nlp.eval.evaluate_summarization import get_rouge

os.environ["NCCL_IB_DISABLE"] = "0"

def shorten_dataset(dataset, top_n=-1, world_size=1, rank=1):
    if world_size == 1:
        if top_n == -1:
            return dataset
        
        return SummarizationNonIterableDataset(dataset.source[0:top_n], dataset.target[0:top_n])
    else:
        if top_n == -1:
            total_len = len(dataset)
        else:
            total_len = top_n
        chunk_size = total_len/world_size
        start = int((rank)*chunk_size)
        if rank == world_size:
            end = total_len
        else:
            end = int((rank+1)*chunk_size)
        return SummarizationNonIterableDataset(dataset.source[start:end], dataset.target[start:end])

parser = argparse.ArgumentParser()
parser.add_argument("--rank", type=int, default=0,
                    help="The rank of the current node in the cluster")
parser.add_argument("--dist_url", type=str, default="tcp://127.0.0.1:29500",
                    help="URL specifying how to initialize the process groupi.")
parser.add_argument("--node_count", type=int, default=1,
                    help="Number of nodes in the cluster.")

parser.add_argument("--cache_dir", type=str, default="./abstemp",
                    help="Directory to cache the tokenizer.")
parser.add_argument("--data_dir", type=str, default="./",
                    help="Directory to download the preprocessed data.")
parser.add_argument("--output_dir", type=str, default="./abstemp",
                    help="Directory to save the output model and prediction results.")
parser.add_argument("--batch_size", type=int, default=64,
                    help="batch size in terms of input token numbers in training")
parser.add_argument("--summary_filename", type=str, default="generated_summaries.txt", 
                    help="Summary file name generated by prediction for evaluation.")
parser.add_argument("--model_filename", type=str, default="dist_extsum_model.pt", 
                    help="model file name saved for evaluation.")
parser.add_argument("--top_n", type=int, default=64, 
                    help="top number of test examples used in evalation")
parser.add_argument("--fp16", type=str.lower, default='false', choices=['true', 'false'],
                    help="Whether to use half-precision model in prediction")

def load_processed_cnndm_abs(data_path, 
        train_file= "train_abssum_dataset_full.pt", 
        test_file="test_abssum_dataset_full.pt" ):
    #TOP_N = -1
    train_data_path = os.path.join(data_path, train_file)
    test_data_path = os.path.join(data_path, test_file)
    train_sum_dataset = torch.load(train_data_path)
    test_sum_dataset = torch.load(test_data_path)
    return train_sum_dataset, test_sum_dataset


def main(prediction_result):
    args = parser.parse_args()

    print("NCCL_IB_DISABLE: {}".format(os.getenv("NCCL_IB_DISABLE")))
    print("output_dir is {}".format(args.output_dir))
    print("data_dir is {}".format(args.data_dir))
    print("cache_dir is {}".format(args.cache_dir))
    print("top_n is {}".format(args.top_n))

    ngpus_per_node = torch.cuda.device_count()
    processor = AbsSumProcessor(cache_dir=args.cache_dir)
    summarizer = AbsSum(
        processor, cache_dir=args.cache_dir, test=True
    )
    checkpoint = (torch.load(os.path.join(args.output_dir, args.model_filename), map_location='cpu'))
    summarizer.model.load_checkpoint(checkpoint['model'])
    mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, summarizer, prediction_result,  args))


def main_worker(local_rank, ngpus_per_node, summarizer, prediction_result, args):
    rank = args.rank * ngpus_per_node + local_rank
    world_size = args.node_count * ngpus_per_node
    print("world_size is {}".format(world_size))
    print("local_rank is {} and rank is {}".format(local_rank, rank))
    
    
    torch.distributed.init_process_group(
        backend="nccl",
        init_method=args.dist_url,
        world_size=world_size,
        rank=rank,
      )

    train_sum_dataset, test_sum_dataset = load_processed_cnndm_abs(args.data_dir)

    if rank not in [-1, 0]:
        save_every = -1
        this_validate = None
    else:
        save_every = 500

    #TOP_N = 128
    #if args.quick_run.lower() == "false":
    TOP_N = args.top_n

    start = time.time()

    prediction = summarizer.predict(shorten_dataset(test_sum_dataset, top_n=TOP_N, world_size=world_size, rank=rank),
                batch_size=args.batch_size, num_gpus=None, local_rank=local_rank)
    prediction_result.append((rank,prediction))
    #print(prediction[0])

    end = time.time()
    print("rank {0}, duration {1:.6f}s".format(rank, end - start))
    # only use the following line when you use your own cluster. 
    # AML distributed training run cleanup for you.
    dist.destroy_process_group()

if __name__ == "__main__":
    with Manager() as manager:
        args = parser.parse_args()
        train_sum_dataset, test_sum_dataset = load_processed_cnndm_abs(args.data_dir)
        summaries = [" ".join(summary).rstrip() for _, summary in test_sum_dataset]
        print("summaries_length is {}".format(len(summaries)))
        test_results = manager.list()
        main(test_results)
        results = deepcopy(test_results)
        new_list = sorted(results, key=lambda x: x[0])
        # print(new_list)
        predictions = []
        for i in new_list:
            predictions.extend(i[1])
        def _write_list_to_file(list_items, filename):
            with open(filename, "w") as filehandle:
                # for cnt, line in enumerate(filehandle):
                for item in list_items:
                    filehandle.write("%s\n" % item)

        print("writing generated summaries")
        _write_list_to_file(predictions, os.path.join(args.output_dir, args.summary_filename))

        RESULT_DIR = TemporaryDirectory().name
        rouge_score = get_rouge(predictions, summaries[0:len(predictions)], RESULT_DIR)
        print(rouge_score)

