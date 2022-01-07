import sys
sys.path += ['./']
import os
import torch
import gzip
import csv
import pickle
import argparse
import json
import glob
import numpy as np

from utils.util import pad_input_ids, multi_file_process, numbered_byte_file_generator, EmbeddingCache
from model.models import MSMarcoConfigDict, ALL_MODELS
from torch.utils.data import DataLoader, Dataset, TensorDataset, IterableDataset, get_worker_info

from os import listdir
from os.path import isfile, join
'''
    Data Preprocessing
'''
def write_query_rel(args, pid2offset, query_file, positive_id_file, out_query_file, out_id_file):
    print("Writing query files " + str(out_query_file) + " and " + str(out_id_file))
    '''
        query positive id (relevant)
    '''
    query_positive_id = set()
    query_positive_id_path = os.path.join(args.data_dir, positive_id_file,)
    print("Loading query_to_positive_doc_id")
    with gzip.open(query_positive_id_path, 'rt', encoding='utf8') if positive_id_file[-2:] == "gz" else open(query_positive_id_path, 'r', encoding='utf8') as f:
        if args.data_type == 0:
            tsvreader = csv.reader(f, delimiter=" ")
        else:
            tsvreader = csv.reader(f, delimiter="\t")
        for [topicid, _, docid, rel] in tsvreader:
            query_positive_id.add(int(topicid))
    '''
        query
    '''
    query_collection_path = os.path.join(args.data_dir, query_file,) 
    out_query_path = os.path.join(args.out_data_dir, out_query_file,)

    qid2offset = {}
    print('start query file split processing')
    multi_file_process(args, 32, query_collection_path, out_query_path, QueryPreprocessingFn)
    
    print('start merging splits')
    idx = 0
    with open(out_query_path, 'wb') as f:
        # transfer each line to "p_id.to_bytes(8, 'big') + passage_len.to_bytes(4, 'big') + content=np.array(input_id_b, np.int32).tobytes()"
        for record in numbered_byte_file_generator(out_query_path, 32, 8 + 4 + args.max_query_length * 4):
            q_id = int.from_bytes(record[:8], 'big')
            ####
            if q_id not in query_positive_id: # query_positive_id is a set 
                # exclude the query as it is not in label set
                continue
            ####
            f.write(record[8:]) # exclude q_id, only save q_len, q_content
            qid2offset[q_id] = idx
            idx += 1
            if idx < 3:
                print(str(idx) + " " + str(q_id))
    # qid2offset info
    qid2offset_path = os.path.join(args.out_data_dir, "qid2offset.pickle",)
    with open(qid2offset_path, 'wb') as handle:
        pickle.dump(qid2offset, handle, protocol=4)
    print("done saving qid2offset")
    # query info
    print("Total lines written: " + str(idx))
    meta = {'type': 'int32', 'total_number': idx, 'embedding_size': args.max_query_length}
    with open(out_query_path + "_meta", 'w') as f:
        json.dump(meta, f)
    
    # embedding cache
    embedding_cache = EmbeddingCache(out_query_path)
    with embedding_cache as emb:
        print("Query embedding cache first line", emb[0])
    
    # saving positive id
    out_id_path = os.path.join(args.out_data_dir, out_id_file,)
    print("Writing qrels")
    # write down: str(qid2offset[topicid]) + "\t" + str(pid2offset[docid]) + "\t" + rel + "\n"
    with gzip.open(query_positive_id_path, 'rt', encoding='utf8') if positive_id_file[-2:] == "gz" else open(query_positive_id_path, 'r', encoding='utf8') as f, \
            open(out_id_path, "w", encoding='utf-8') as out_id:
        if args.data_type == 0:
            tsvreader = csv.reader(f, delimiter=" ")
        else:
            tsvreader = csv.reader(f, delimiter="\t")
        out_line_count = 0

        for [topicid, _, docid, rel] in tsvreader:
            topicid = int(topicid)
            if args.data_type == 0:
                docid = int(docid[1:])
            else:
                docid = int(docid)
            out_id.write(str(qid2offset[topicid]) + "\t" + str(pid2offset[docid]) + "\t" + rel + "\n")
            out_line_count += 1
        print("Total lines written: " + str(out_line_count))

def preprocess(args):
    args.data_dir = os.path.join(args.data_dir, "doc") if args.data_type == 0 else os.path.join(args.data_dir, "passage")
    args.out_data_dir = args.out_data_dir + "_{}_{}_{}".format(args.model_name_or_path, args.max_seq_length, args.data_type)
    
    if not os.path.exists(args.out_data_dir):
        os.makedirs(args.out_data_dir)
    '''
        passage
    '''
    # input dataset path
    if args.data_type == 0:
        in_passage_path = os.path.join(args.data_dir, "msmarco-docs.tsv",) # MSMARCO/doc
    else: 
        in_passage_path = os.path.join(args.data_dir, "collection.tsv",) # MSMARCO/passage
    # output dataset path
    out_passage_path = os.path.join(args.out_data_dir, "passages",) # raw_data/ann_data_tokenizer_seqlen/passages
    if os.path.exists(out_passage_path): # out_passage_path is file not dir
        print("preprocessed data already exist, exit preprocessing")
        return

    print('start passage file split processing') 
    multi_file_process(args, 32, in_passage_path, out_passage_path, PassagePreprocessingFn) # 32 is the number of processing

    print('start merging splits')
    # read each record by bytes then use int.from_bytes to recover integer number
    pid2offset = {}
    out_line_count = 0
    with open(out_passage_path, 'wb') as f:
        # transfer each line to "p_id.to_bytes(8, 'big') + passage_len.to_bytes(4, 'big') + content=np.array(input_id_b, np.int32).tobytes()"
        for idx, record in enumerate(numbered_byte_file_generator(out_passage_path, 32, 8 + 4 + args.max_seq_length * 4)):
            p_id = int.from_bytes(record[:8], 'big') # p_id: 8 bytes encoder
            f.write(record[8:]) # saved by bytes
            pid2offset[p_id] = idx
            if idx < 3:
                print(str(idx) + " " + str(p_id))
            out_line_count += 1
    print("Total lines written: " + str(out_line_count))
    
    # data proprecessig meta info
    meta = {'type': 'int32', 'total_number': out_line_count, 'embedding_size': args.max_seq_length}
    with open(out_passage_path + "_meta", 'w') as f:
        json.dump(meta, f)
    embedding_cache = EmbeddingCache(out_passage_path)
    with embedding_cache as emb:
        print("Passage embedding cache first line", emb[0])
    # data pid2offset info
    pid2offset_path = os.path.join(args.out_data_dir, "pid2offset.pickle",)
    with open(pid2offset_path, 'wb') as handle:
        pickle.dump(pid2offset, handle, protocol=4) # save dictionary in pickle, {p_id:idx} p_id is the id of document, idx is the index
    print("done saving pid2offset")
    
    '''
        query
    '''
    # start processing
    # pid2offset, query_file, positive_id_file, out_query_file, out_id_file
    if args.data_type == 0:
        write_query_rel(args, pid2offset, "msmarco-doctrain-queries.tsv", "msmarco-doctrain-qrels.tsv", "train-query", "train-qrel.tsv")
        write_query_rel(args, pid2offset, "msmarco-test2019-queries.tsv", "2019qrels-docs.txt", "dev-query", "dev-qrel.tsv")
    else:
        write_query_rel(args, pid2offset, "queries.train.tsv", "qrels.train.tsv", "train-query", "train-qrel.tsv")
        write_query_rel(args, pid2offset, "queries.dev.small.tsv", "qrels.dev.small.tsv", "dev-query", "dev-qrel.tsv")

    # remove *_split* files
    for split_file in glob.glob(os.path.join(args.out_data_dir, '*_split*')):
        print("remove %s" % split_file)
        os.remove(split_file)

# process each line from file
# transfer each line to "p_id.to_bytes(8, 'big') + passage_len.to_bytes(4, 'big') + content=np.array(input_id_b, np.int32).tobytes()"
def PassagePreprocessingFn(args, line, tokenizer):
    if args.data_type == 0:
        line_arr = line.split('\t')
        p_id = int(line_arr[0][1:])  # remove "D"

        url = line_arr[1].rstrip()
        title = line_arr[2].rstrip()
        p_text = line_arr[3].rstrip()

        #full_text = url + "<sep>" + title + "<sep>" + p_text
        full_text = url + " "+tokenizer.sep_token+" " + title + " "+tokenizer.sep_token+" " + p_text
        # keep only first 10000 characters, should be sufficient for any
        # experiment that uses less than 500 - 1k tokens
        full_text = full_text[:args.max_doc_character]
    else:
        line = line.strip()
        line_arr = line.split('\t')
        p_id = int(line_arr[0])

        p_text = line_arr[1].rstrip()

        # keep only first 10000 characters, should be sufficient for any
        # experiment that uses less than 500 - 1k tokens
        full_text = p_text[:args.max_doc_character]
    # tokenizer.encode: using vocab.txt from BERT change token to dict_id, and add 101=[cls] and 102=[sep] in the before and after passage
    passage = tokenizer.encode(full_text, add_special_tokens=True, max_length=args.max_seq_length,) # return token id
    passage_len = min(len(passage), args.max_seq_length)
    # expand passage with max length by using tokenizer.pad_token_id
    input_id_b = pad_input_ids(passage, args.max_seq_length, pad_token=tokenizer.pad_token_id) # keep the same seq length by padding

    return p_id.to_bytes(8, 'big') + passage_len.to_bytes(4, 'big') + np.array(input_id_b, np.int32).tobytes()

# process each line from file
def QueryPreprocessingFn(args, line, tokenizer):
    line_arr = line.split('\t')

    q_id = int(line_arr[0])
    q_text = line_arr[1].rstrip()

    passage = tokenizer.encode(q_text, add_special_tokens=True, max_length=args.max_query_length)
    passage_len = min(len(passage), args.max_query_length)
    # expand passage with max length by using tokenizer.pad_token_id
    input_id_b = pad_input_ids(passage, args.max_query_length, pad_token=tokenizer.pad_token_id)

    return q_id.to_bytes(8,'big') + passage_len.to_bytes(4, 'big') + np.array(input_id_b, np.int32).tobytes()

###################################################################################
'''
    DataLoad Generation
'''
def GetProcessingFn(args, query=False):
    def fn(vals, i): # i: id
        passage_len, passage = vals
        max_len = args.max_query_length if query else args.max_seq_length
        """
        Args:
            input_ids: Indices of input sequence tokens in the vocabulary.
            attention_mask: Mask to avoid performing attention on padding token indices.
                Mask values selected in ``[0, 1]``:
                Usually  ``1`` for tokens that are NOT MASKED, ``0`` for MASKED (padded) tokens.
            token_type_ids: Segment token indices to indicate first and second portions of the inputs.
            label: Label corresponding to the input
        """
        pad_len = max(0, max_len - passage_len)
        token_type_ids = ([0] if query else [1]) * passage_len + [0] * pad_len
        attention_mask = [1] * passage_len + [0] * pad_len
        # id, passage_each_token_id, [1,1,1, ..., 0,0,0], [0,0,0, ..., 0,0,0]/[1,1,1, ..., 0,0,0]
        passage_collection = [(i, passage, attention_mask, token_type_ids)] 

        # change input into torch.tensor format
        query2id_tensor = torch.tensor([f[0] for f in passage_collection], dtype=torch.long) # [id]
        all_input_ids_a = torch.tensor([f[1] for f in passage_collection], dtype=torch.int) # [passage_each_token_id]
        all_attention_mask_a = torch.tensor([f[2] for f in passage_collection], dtype=torch.bool) # [1,1,1, ..., 0,0,0]
        all_token_type_ids_a = torch.tensor([f[3] for f in passage_collection], dtype=torch.uint8) # [0,0,0, ..., 0,0,0]/[1,1,1, ..., 0,0,0]
        # passage_each_token_id, [1,1,1, ..., 0,0,0], [0,0,0, ..., 0,0,0]/[1,1,1, ..., 0,0,0], id
        # zip a, b, c, d, https://blog.csdn.net/qq_40211493/article/details/107529148
        dataset = TensorDataset(all_input_ids_a, all_attention_mask_a, all_token_type_ids_a, query2id_tensor)

        return [ts for ts in dataset] # [[a,b,c,d], ...]

    return fn

def GetTrainingDataProcessingFn(args, query_cache, passage_cache):
    def fn(line, i):
        line_arr = line.split('\t')
        
        qid = int(line_arr[0])
        pos_pid = int(line_arr[1])
        neg_pids = line_arr[2].split(',')
        neg_pids = [int(neg_pid) for neg_pid in neg_pids]

        all_input_ids_a = []
        all_attention_mask_a = []

        query_data = GetProcessingFn(args, query=True)(query_cache[qid], qid)[0]
        pos_data = GetProcessingFn(args, query=False)(passage_cache[pos_pid], pos_pid)[0]

        pos_label = torch.tensor(1, dtype=torch.long)
        neg_label = torch.tensor(0, dtype=torch.long)

        for neg_pid in neg_pids:
            neg_data = GetProcessingFn(args, query=False)(passage_cache[neg_pid], neg_pid)[0]
            yield (query_data[0], query_data[1], query_data[2], pos_data[0], pos_data[1], pos_data[2], pos_label)
            yield (query_data[0], query_data[1], query_data[2], neg_data[0], neg_data[1], neg_data[2], neg_label)

    return fn

def GetTripletTrainingDataProcessingFn(args, query_cache, passage_cache):
    # query_cache: [(len, [id1, id2, id3, ....., 1, 1, 1, 1]), ...]
    # passage_cache: [(len, [id1, id2, id3, ....., 1, 1, 1, 1]), ...]
    def fn(line, i): # ann data: for i, line in enumerate(ann_data.readlines())
        line_arr = line.split('\t')
        # qid, pos_pid, neg_pids (token index in the dictionary)
        qid = int(line_arr[0])
        pos_pid = int(line_arr[1])
        neg_pids = line_arr[2].split(',')
        neg_pids = [int(neg_pid) for neg_pid in neg_pids]

        all_input_ids_a = []
        all_attention_mask_a = []

        query_data = GetProcessingFn(args, query=True)(query_cache[qid], qid)[0] # [a,b,c,d]
        pos_data = GetProcessingFn(args, query=False)(passage_cache[pos_pid], pos_pid)[0]

        for neg_pid in neg_pids:
            neg_data = GetProcessingFn(args, query=False)(passage_cache[neg_pid], neg_pid)[0]
            yield (query_data[0], query_data[1], query_data[2], pos_data[0], pos_data[1], pos_data[2],
                   neg_data[0], neg_data[1], neg_data[2]) # qid, pos_pid, and neg_pid are not needed. 

    return fn

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", default="./data/MSMARCO", type=str, help="The input data dir",)
    parser.add_argument("--out_data_dir", default="./data/MSMARCO/ann_data", type=str, help="The output data dir",)
    parser.add_argument("--model_type", default="rdot_nll", type=str, help="Model type selected in the list: " + ", ".join(MSMarcoConfigDict.keys()),)
    parser.add_argument("--model_name_or_path", default="roberta-base", type=str, help="Path to pre-trained model or shortcut name selected in the list: " +", ".join(ALL_MODELS),)
    parser.add_argument("--max_seq_length", default=2048, type=int, help="The maximum total input sequence length after tokenization. Sequences longer ""than this will be truncated, sequences shorter will be padded.",)
    parser.add_argument("--max_query_length", default=64, type=int, help="The maximum total input sequence length after tokenization. Sequences longer ""than this will be truncated, sequences shorter will be padded.",)
    parser.add_argument("--max_doc_character", default=10000, type=int, help="used before tokenizer to save tokenizer latency",)
    parser.add_argument("--data_type", default=1, type=int, help="0 for doc, 1 for passage",)
    args = parser.parse_args()

    preprocess(args)


if __name__ == '__main__':
    main()
