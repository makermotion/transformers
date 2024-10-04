import torch
import torch.nn as nn
import torch.optim as optim
from pydantic import BaseModel
from tqdm import tqdm

from gpt import GPT
from tokenization import Tokenizer


class ModelConfig(BaseModel):
    embed_dim: int = 512
    tgt_vocab_size: int = 4096
    seq_len: int = 256
    num_layers: int = 6
    expansion_factor: int = 6
    n_heads: int = 8


class DatasetConfig(BaseModel):
    batch_size: int = 32
    shuffle: bool = True


class Dataset(torch.utils.data.Dataset):

    def __init__(self, data, seq_len, tokenizer):
        self.seq_len = seq_len - 2
        self.tokenizer = tokenizer
        self.data = self.tokenizer.encode(data)

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        if idx + self.seq_len + 1 > len(self.data):
            idx = 0


#        src = self.tokenizer.add_special_tokens(self.data[idx:idx +
#                                                          self.seq_len])
#        tgt = self.tokenizer.add_special_tokens(self.data[idx + 1:idx +
#                                                          self.seq_len + 1])
        src = self.data[idx:idx + self.seq_len]
        tgt = self.data[idx + 1:idx + self.seq_len + 1]
        return torch.tensor(src).to("cuda"), torch.tensor(tgt).to("cuda")


def evaluate(model, criterion, eval_loader, vocab_size):
    model.eval()
    total_loss = 0
    with tqdm(eval_loader, unit="iter") as tepoch:
        with torch.no_grad():
            for src, tgt in tepoch:
                mask = model.make_tgt_mask(tgt).to("cuda")
                output = model(src, mask)
                loss = criterion(output.view(-1, vocab_size), tgt.view(-1))
                tepoch.set_postfix(eval_loss={loss.item()})
                total_loss += loss.item()
    return total_loss / len(eval_loader)


def main():
    # Load tokenizer
    tokenizer = Tokenizer.load("toy_data/python_book")

    # Model configuration
    model_config = ModelConfig()
    model = GPT(**model_config.dict()).to("cuda")

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(),
                           lr=0.0001,
                           betas=(0.9, 0.98),
                           eps=1e-9)

    # Load data and create DataLoader
    with open("toy_data/python_book.txt", encoding="utf-8") as f:
        data = f.read()

    with open("toy_data/python_book_eval.txt", encoding="utf-8") as f:
        data_eval = f.read()

    dataset_config = DatasetConfig()
    train_loader = torch.utils.data.DataLoader(
        Dataset(data, model_config.seq_len, tokenizer),
        batch_size=dataset_config.batch_size,
        shuffle=dataset_config.shuffle)

    eval_loader = torch.utils.data.DataLoader(
        Dataset(data_eval, model_config.seq_len, tokenizer),
        batch_size=dataset_config.batch_size,
        shuffle=dataset_config.shuffle)

    # Training loop
    for epoch in range(100):
        train_loss = 0
        model.train()
        with tqdm(train_loader, unit="iter") as tepoch:
            for src, tgt in tepoch:
                mask = model.make_tgt_mask(tgt).to("cuda")
                optimizer.zero_grad()
                output = model(src, mask)
                loss = criterion(output.view(-1, model_config.tgt_vocab_size),
                                 tgt.view(-1))
                loss.backward()
                optimizer.step()
                tepoch.set_postfix(loss={loss.item()})
                train_loss += loss.item()

            train_loss /= len(train_loader)

        eval_loss = evaluate(model, criterion, eval_loader,
                             model_config.tgt_vocab_size)
        print(
            f"Epoch {epoch} | Train Loss: {train_loss} | Eval Loss: {eval_loss}"
        )
        if epoch % 5 == 0:
            torch.save(model.state_dict(), f'gpt_epoch_{epoch}.pth')
            print('Model saved!')


if __name__ == "__main__":
    main()
