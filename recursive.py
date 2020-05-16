import torch
import torch.nn as nn

class RNNOp(nn.Module):
    def __init__(self, nhid, dropout=0.):
        super(RNNOp, self).__init__()
        self.op = nn.Sequential(
            nn.Linear(2 * nhid, nhid),
            nn.Tanh(),
            nn.Dropout(dropout),
        )

    def forward(self, left, right):
        return self.op(torch.cat([left, right], dim=-1))

class LSTMOp(nn.Module):
    def __init__(self, nhid, dropout=0.):
        super(LSTMOp, self).__init__()
        assert(nhid % 2 == 0)
        self.hidden_size = nhid // 2
        self.transform = nn.Linear(nhid, 5 * (nhid // 2))
        self.dropout = nn.Dropout(dropout)

    def forward(self, left:torch.Tensor, right:torch.Tensor):
        l_h, l_c = left.chunk(2, dim=-1)
        r_h, r_c = right.chunk(2, dim=-1)

        h = torch.cat([l_h, r_h], dim=-1)
        lin_gates, lin_in_c = self.transform(h).split(
            (4 * self.hidden_size, self.hidden_size), dim=-1)
        i, f1, f2, o = torch.sigmoid(lin_gates).chunk(4, dim=-1)
        in_c = torch.tanh(lin_in_c)
        c = i * in_c + f1 * l_c + f2 * r_c
        h = o * torch.tanh(c)
        return torch.cat((h, c), dim=-1)

class Recursive(nn.Module):
    def __init__(self, op,
                 vocabulary_size,
                 hidden_size, padding_idx,
                 parens_id=(0, 1),
                 dropout=0.):
        super(Recursive, self).__init__()
        self.hidden_size = hidden_size
        self.op = op(hidden_size, dropout=dropout)
        self.padding_idx = padding_idx
        self.embedding = nn.Embedding(vocabulary_size, hidden_size)
        self.paren_open, self.paren_close = parens_id
        self._recurse = Recursive_(
            self.hidden_size, self.op,
            self.padding_idx, self.embedding,
            self.paren_open, self.paren_close
        )
        self.recurse = torch.jit.script(self._recurse)

    def forward(self, input):
        return self.recurse(input)

    def __getstate__(self):
        del self.recurse
        state = self.__dict__
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.recurse = torch.jit.export(self._recurse)


class Recursive_(nn.Module):
    def __init__(self, hidden_size, op, padding_idx,
                 embedding, paren_open, paren_close):
        super(Recursive_, self).__init__()
        self.hidden_size = hidden_size
        self.op = op
        self.padding_idx = padding_idx
        self.embedding = embedding
        self.paren_open, self.paren_close = paren_open, paren_close

    def forward(self, input):
        max_length, batch_size  = input.size()

        # Masking business
        length_mask = input != self.padding_idx
        open_mask = input == self.paren_open
        close_mask = input == self.paren_close
        token_mask = length_mask & (~open_mask) & (~close_mask)
        do_nothing = (open_mask | ~length_mask).all(dim=1)

        # Initialise stack
        stack_height = torch.sum(token_mask, dim=0).max() + 1
        input_emb = self.embedding(input)

        batch_idx = torch.arange(batch_size,
                                 dtype=torch.long, device=input.device)
        stack_ptr = torch.zeros(batch_size,
                                dtype=torch.long, device=input.device)
        stack = torch.zeros(batch_size, stack_height, self.hidden_size,
                            device=input.device)
        for t in range(max_length):
            if not do_nothing[t]:
                stack, stack_ptr = self.step(
                    batch_idx,
                    input_emb[t], token_mask[t], close_mask[t],
                    stack, stack_ptr
                )
        return stack[:, 0]

    def step(self, batch_idx:torch.Tensor, emb_t:torch.Tensor,
             is_token:torch.Tensor, is_close:torch.Tensor,
             stack:torch.Tensor, stack_ptr:torch.Tensor):
        stack_ptr_ = stack_ptr
        stack_ptr = stack_ptr_.clone()

        # shift
        if is_token.any():
            stack_ = stack.index_put((batch_idx, stack_ptr_), emb_t)
            stack[is_token] = stack_[is_token]
            stack_ptr[is_token] = (stack_ptr_ + 1)[is_token]

        # reduce
        if is_close.any():
            r_child = stack[batch_idx, stack_ptr_ - 1]
            l_child = stack[batch_idx, stack_ptr_ - 2]
            parent = self.op(l_child, r_child)
            stack_ = stack.index_put((batch_idx, stack_ptr_ - 2), parent)
            stack[is_close] = stack_[is_close]
            stack_ptr[is_close] = (stack_ptr_ - 1)[is_close]

        return stack, stack_ptr

if __name__ == "__main__":
    tree = Recursive(LSTMOp, 5, 4, padding_idx=4)
    batch_result = tree.forward(torch.tensor([
           [2, 4, 4, 4, 4, 4, 4, 4, 4, 4],
           [0, 0, 2, 3, 1, 0, 2, 2, 1, 1],
           [0, 2, 0, 3, 0, 2, 3, 1, 1, 1]
        ], dtype=torch.long).t())
    assert(torch.allclose(
        batch_result[0],
        tree.embedding(torch.Tensor([[2]]).long().t())[0]))

    embs = tree.embedding(torch.Tensor(([2, 3, 2, 2],)).long().t())
    result = tree.op(tree.op(embs[0], embs[1]),
                     tree.op(embs[2], embs[3]))
    assert(torch.allclose(batch_result[1], result))

    embs = tree.embedding(torch.Tensor([[2, 3, 2, 3 ]]).long().t())
    result = tree.op(embs[0],
                     tree.op(embs[1],
                             tree.op(embs[2], embs[3])))
    assert(torch.allclose(batch_result[2], result))

    batch_result.sum().backward()

    tree = torch.save(tree, 'tree.pt')