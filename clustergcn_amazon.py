import time

import dgl
import dgl.nn as dglnn

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchmetrics.functional as MF
from ogb.nodeproppred import DglNodePropPredDataset
from dgl.data import AsNodePredDataset
from dgl.data import RedditDataset
import GPUtil
import dgl.function as fn
from utils.data import load_data
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import classification_report, f1_score
from ogb.nodeproppred import DglNodePropPredDataset, Evaluator
from dgl import AddSelfLoop


class GNNModel(nn.Module):
    def __init__(self,  gnn_layer: str, n_layers: int, layer_dim: int,
                 input_feature_dim: int, n_classes: int, n_linear: int):
        super().__init__()

        assert n_layers >= 1, 'GNN must have at least one layer'
        dims = [input_feature_dim] + [layer_dim] * (n_layers-1) + [n_classes]
        print(dims)
        
        self.convs = nn.ModuleList()
        self.norm = nn.ModuleList()
        
        self.n_layers = n_layers
        self.n_linear = n_linear
        
        for idx in range(n_layers):
            if idx < n_layers - n_linear:
                if gnn_layer == 'gat':
                    # use 2 aattention heads
                    # layer = dglnn.GATConv(dims[idx], dims[idx+1], 1)  # pylint: disable=no-member
                    layer = dglnn.AGNNConv(learn_beta=False, allow_zero_in_degree=True)
                elif gnn_layer == 'gcn':
                    layer = dglnn.GraphConv(dims[idx], dims[idx+1], allow_zero_in_degree=True)  # pylint: disable=no-member
                elif gnn_layer == 'sage':
                    # Use mean aggregtion
                    # pylint: disable=no-member
                    layer = dglnn.SAGEConv(dims[idx], dims[idx+1],
                                            aggregator_type='mean')
                else:
                    raise ValueError(f'unknown gnn layer type {gnn_layer}')
                self.convs.append(layer)
            else: 
                self.convs.append(nn.Linear(dims[idx], dims[idx+1]))
                
            if idx < n_layers - 1:
                self.norm.append(nn.LayerNorm(dims[idx+1], elementwise_affine=True))
                
            
    def forward(self, graph, features):
        h = features
        for idx in range(self.n_layers):
            
            h = F.dropout(h, p=0)
            if idx < self.n_layers - self.n_linear:
                # graph->graph[idx] for minibatch training
                h = self.convs[idx](graph, h)
                if h.ndim == 3:  # GAT produces an extra n_heads dimension
                    h = h.mean(1)
            else:
                h = self.convs[idx](h)

            if idx < self.n_layers - 1:
                h = self.norm[idx](h)
                h = F.relu(h, inplace=True)
            
        return h


class SAGE(nn.Module):
    def __init__(self, in_feats, n_hidden, n_classes):
        super().__init__()
        self.layers = nn.ModuleList()
        self.layers.append(dglnn.SAGEConv(in_feats, n_hidden, "mean"))
        self.layers.append(dglnn.SAGEConv(n_hidden, n_hidden, "mean"))
        self.layers.append(dglnn.SAGEConv(n_hidden, n_classes, "mean"))
        self.dropout = nn.Dropout(0.5)

    def forward(self, sg, x):
        h = x
        for l, layer in enumerate(self.layers):
            h = layer(sg, h)
            if l != len(self.layers) - 1:
                h = F.relu(h)
                h = self.dropout(h)
        return h


class GCN(nn.Module):
    def __init__(self, in_size, hid_size, out_size):
        super().__init__()
        self.layers = nn.ModuleList()
        # three-layer GraphSAGE-mean
        self.layers.append(dglnn.GraphConv(in_size, hid_size, activation=F.relu, allow_zero_in_degree=True))
        self.layers.append(dglnn.GraphConv(hid_size, hid_size, activation=F.relu, allow_zero_in_degree=True))
        self.layers.append(dglnn.GraphConv(hid_size, out_size, allow_zero_in_degree=True))
        self.dropout = nn.Dropout(0.5)

    def forward(self, sg, x):
        h = x
        for l, layer in enumerate(self.layers):
            h = layer(sg, h)
            if l != len(self.layers) - 1:
                h = F.relu(h)
                h = self.dropout(h)
        return h
    

# amazon
data = load_data('amazon', multilabel=True)
transform = (AddSelfLoop()) 
g = data[2]
g = transform(g)
num_classes = data[0]

device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")

g.ndata['label'] = g.ndata['label']
g.ndata['train_mask'] = g.ndata['train_mask'].bool()
g.ndata['val_mask'] = g.ndata['val_mask'].bool()
g.ndata['test_mask'] = g.ndata['test_mask'].bool()  
feats = g.ndata['feat']
scaler = StandardScaler()
scaler.fit(feats[g.ndata['train_mask']])
feats = scaler.transform(feats)
g.ndata['feat'] = torch.tensor(feats, dtype=torch.float)

        
model = GNNModel('gcn', 3, 128, g.ndata['feat'].size(1), num_classes, 0).to("cuda:1")
opt = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)

print("sampling!")

# clustergcn
num_partitions = 1000
sampler = dgl.dataloading.ClusterGCNSampler(
    g,
    num_partitions,
    cache_path='cluster_gcn_amazon.pkl',
    prefetch_ndata=["feat", "label", "train_mask", "val_mask", "test_mask"],
)
# DataLoader for generic dataloading with a graph, a set of indices (any indices, like
# partition IDs here), and a graph sampler.
dataloader = dgl.dataloading.DataLoader(
    g,
    torch.arange(num_partitions).to("cuda:1"),
    sampler,
    device="cuda:1",
    batch_size=100,
    shuffle=True,
    drop_last=False,
    num_workers=0,
    use_uva=True,
)

# SAINT
# num_partitions = 50
# sampler = dgl.dataloading.SAINTSampler(mode='node', budget=4500)
# # Assume g.ndata['feat'] and g.ndata['label'] hold node features and labels
# dataloader = dgl.dataloading.DataLoader(
#     g,
#     torch.arange(num_partitions).to("cuda:1"),
#     sampler,
#     device="cuda:1",
#     use_uva=True,
# )

durations = []
best_val_acc = 0
peak_memory = 0
for epoch in range(100):
    t0 = time.time()
    model.train()
    for it, sg in enumerate(dataloader):
        x = sg.ndata["feat"]
        y = sg.ndata["label"]
        m = sg.ndata["train_mask"].bool()
        y_hat = model(sg, x)
        if y.dim() == 1:
            loss = F.cross_entropy(y_hat[m], y[m])
        else:
            loss = F.binary_cross_entropy_with_logits(y_hat[m], y[m], reduction='sum')
        opt.zero_grad()
        loss.backward()
        opt.step()
        if it % 20 == 0:
            # y_hat[y_hat > 0] = 1
            # y_hat[y_hat <= 0] = 0
            # acc = f1_score(y.cpu(), y_hat.cpu(), average='micro')
            GPUs = GPUtil.getGPUs()
            if GPUs[0].memoryUsed > peak_memory:
                peak_memory = GPUs[0].memoryUsed
            print("Loss", loss.item(), "GPU Mem", peak_memory, "MB")

    tt = time.time() - t0
    print("Run time for epoch# %d: %.2fs" % (epoch, tt))
    durations.append(tt)

    model.eval()
    with torch.no_grad():
        val_preds, test_preds = [], []
        val_labels, test_labels = [], []
        for it, sg in enumerate(dataloader):
            x = sg.ndata["feat"]
            y = sg.ndata["label"]
            m_val = sg.ndata["val_mask"].bool()
            y_hat = model(sg, x)
            y_hat[y_hat > 0] = 1
            y_hat[y_hat <= 0] = 0
            val_preds.append(y_hat[m_val])
            val_labels.append(y[m_val])
        val_preds = torch.cat(val_preds, 0)
        val_labels = torch.cat(val_labels, 0)
        val_acc = f1_score(val_labels.cpu(), val_preds.cpu(), average='micro')
        if val_acc.item() > best_val_acc:
            best_val_acc = val_acc.item()
        print("Validation acc:", val_acc.item())

print(
    "Average run time for last %d epochs: %.2fs standard deviation: %.3f"
    % ((epoch - 3), np.mean(durations[4:]), np.std(durations[4:]))
)
print("Best validation acc:", best_val_acc)