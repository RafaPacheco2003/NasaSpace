# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude


import numpy as np
import pandas as pd
import torch
from astropy.coordinates import SkyCoord
import astropy.units as u

from torch.utils.data import Dataset

class AsteroidDataset(Dataset):
    def __init__(self, csv_path, seq_len=3):
        df = csv_path.copy() if isinstance(csv_path, pd.DataFrame) else pd.read_csv(csv_path)
        self.samples = []
        self.seq_len = seq_len
        self.references = []

        for asteroid_id, group in df.groupby("number_mp"):
            group = group.sort_values("epoch")

            values = group[["epoch", "ra", "dec"]].to_numpy()

            for i in range(len(values) - seq_len):
                past = values[i:i + seq_len]
                future = values[i + seq_len]

                ra0, dec0 = past[-1, 1:3]

                reference = SkyCoord(ra0 * u.deg, dec0 * u.deg)
                frame = reference.skyoffset_frame()

                def offsets(rows):
                    sky = SkyCoord(
                        ra=rows[:, 1] * u.deg,
                        dec=rows[:, 2] * u.deg
                    )

                    local = sky.transform_to(frame)

                    return np.column_stack((
                        local.lon.deg,
                        local.lat.deg
                    ))

                # Local angular offsets
                positions = offsets(past)
                target = offsets(future[None, :])[0]

                # Elapsed time between observations (days)
                dt = np.diff(
                    past[:, 0],
                    prepend=past[0, 0]
                )

                # Forecast horizon
                future_dt = (
                    future[0] - past[-1, 0]
                )

                max_gap_days = 3.0

                if np.any(np.diff(past[:, 0]) > max_gap_days):
                    continue

                if future_dt <= 0 or future_dt > max_gap_days:
                    continue

                if np.linalg.norm(target) > 5.0:
                    continue

                X = np.column_stack((positions, dt))

                self.samples.append((
                    X.astype(np.float32),
                    np.float32(future_dt),
                    target.astype(np.float32)
                ))

                self.references.append(reference)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        X, horizon, target = self.samples[idx]

        return (
            torch.from_numpy(X),
            torch.tensor([horizon]),
            torch.from_numpy(target)
        )
