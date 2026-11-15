
import torch
import torch.nn as nn


class AsteroidLSTM(nn.Module):
    def __init__(self, input_size=3, hidden_size=64):
        super().__init__()

        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=1,
            batch_first=True
        )

        self.mlp = nn.Sequential(
            nn.Linear(hidden_size + 1, 64),
            nn.LayerNorm(64),
            nn.LeakyReLU(),
            nn.Dropout(0.2),

            nn.Linear(64, 32),
            nn.LayerNorm(32),
            nn.LeakyReLU(),
            nn.Dropout(0.1),

            nn.Linear(32, 2)
        )

    def forward(self, X, horizon):
        # X: (B, T, 3)
        # horizon: (B, 1)

        _, (hidden, cell) = self.lstm(X)

        # Last hidden state: (B, 64)
        h = hidden[-1]

        features = torch.cat((h, horizon), dim=1)

        return self.mlp(features)
