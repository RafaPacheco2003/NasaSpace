
from astroquery.gaia import Gaia
from pathlib import Path
import pandas as pd
import time

OUTPUT = Path("data/gaia_asteroids.csv")
OUTPUT.parent.mkdir(exist_ok=True)

Gaia.ROW_LIMIT = -1

completed = set()
if OUTPUT.exists():
    completed = set(
        pd.read_csv(OUTPUT, usecols=["number_mp"])["number_mp"]
    )

for asteroid_id in range(1, 101):
    if asteroid_id in completed:
        continue

    query = f"""
    SELECT TOP 1000
        number_mp, transit_id, epoch, ra, dec
    FROM gaiadr3.sso_observation
    WHERE number_mp = {asteroid_id}
    """

    for attempt in range(3):
        try:
            print(f"Downloading asteroid {asteroid_id}...")

            data = Gaia.launch_job_async(query).get_results()
            df = data.to_pandas()

            if df.empty:
                print("No observations")
                break

            df = (
                df.groupby(
                    ["number_mp", "transit_id"],
                    as_index=False
                )
                .agg(
                    epoch=("epoch", "mean"),
                    ra=("ra", "mean"),
                    dec=("dec", "mean"),
                    measurements=("ra", "size")
                )
                .sort_values("epoch")
            )

            df.to_csv(
                OUTPUT,
                mode="a",
                header=not OUTPUT.exists(),
                index=False
            )

            print(f"Saved {len(df)} transits")
            break

        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            time.sleep(3 * (attempt + 1))

    time.sleep(1)

print("Finished:", OUTPUT)
