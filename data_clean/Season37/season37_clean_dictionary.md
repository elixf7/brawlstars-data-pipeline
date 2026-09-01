# Data Dictionary – `season37_clean.db`

This document describes the **`matches`** table created by `data_clean/build_clean_db.py`.
Each row represents one match from Season 37 with every brawler flattened into dedicated
columns for easy querying.

| #     | Column name       | Type                | Example / Notes                                            |
| ----- | ----------------- | ------------------- | ---------------------------------------------------------- |
| 1     | `id`              | INTEGER PRIMARY KEY | 587342                                                     |
| 2     | `battle_time`     | TEXT (ISO-8601 UTC) | `20250507T141312.000Z`                                     |
| 3     | `mode`            | TEXT                | `heist`                                                    |
| 4     | `map`             | TEXT                | `Bridge Too Far`                                           |
| 5     | `record`          | TEXT                | `T1-T2-T1` – dash-separated round winners (best-of-3)      |
| 6     | `star_brawler`    | TEXT                | `MANDY`                                                    |
| 7     | `star_power`      | INTEGER             | 11                                                         |
| 8     | `star_player_tag` | TEXT                | `#C8CL8U0YL`                                               |
| 9     | `star_elo`        | INTEGER             | 14                                                         |
| 10    | `avg_elo`         | REAL                | 13.67 – mean elo across **all six** brawlers in this match |
| 11-25 | `t1_b{0-2}_*`     | see below           | Attributes for Team 1 brawlers, repeated for slots 0-2     |
| 26-40 | `t2_b{0-2}_*`     | see below           | Same for Team 2                                            |

### Per-brawler sub-columns (repeated for each `t{team}_b{slot}_` prefix)

| Suffix             | Type    | Description                                    |
| ------------------ | ------- | ---------------------------------------------- |
| `name`             | TEXT    | Brawler name, **upper-case** (null if missing) |
| `elo`              | INTEGER | Elo at match time                              |
| `rank`             | INTEGER | Season rank (may be null)                      |
| `highest_trophies` | INTEGER | Lifetime max trophies (may be null)            |
| `power`            | INTEGER | Power level (1-11)                             |

For example, `t1_b0_name = "BELLE"`, `t2_b2_power = 11`.

At present the table has **40 columns** (10 core + 30 brawler attributes) and
~1.32 million rows.

---

## Label definition used in the model

The `record` column encodes which team won each round. For training we derive
a single binary label:

```
 label = 1  if  count("T1") > count("T2")  else 0
```

This matches the semantics “Team 1 wins the match”.

---

## Typical query pattern

```sql
SELECT mode, map, avg_elo,
       t1_b0_name, t1_b1_name, t1_b2_name,
       t2_b0_name, t2_b1_name, t2_b2_name,
       record
FROM matches
WHERE mode = 'heist' AND battle_time BETWEEN '20250501' AND '20250531';
```

---

_Last updated: {{DATE}}_
