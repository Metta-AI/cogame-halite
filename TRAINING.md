# Halite post-training

`tools/export_posttrain.py` plays ten complete native games per certified
variant. At each directive turn it captures the hosted system prompt and full
seat observation. The shipped tidewalker and corsair policies supply replies
accepted by the production parser. All four seats choose against one pre-turn
state, then the production simulator advances. Whole games stay in one split.

```sh
uv run python tools/test_posttrain.py
uv run python tools/export_posttrain.py /tmp/halite-data 10 standard
```

The other certified variants are `sprint` and `richfields`. Ten games yielded
640 training and 160 validation decisions for each. The largest examples used
2,455, 2,436, and 2,449 tokens with a local Qwen2.5 tokenizer, all within
4,096 tokens. One CPU optimizer step on a tiny local model reduced validation
loss from 5.5696 to 5.4912, 5.5111 to 5.4343, and 5.4825 to 5.3974,
respectively. These short runs verify the training path, not policy quality.

From a Metta checkout with `metta-posttrain` installed:

```sh
uv run --package metta-posttrain --extra train python -m metta_posttrain.train \
  --dataset /tmp/halite-data --output /tmp/halite-adapter \
  --model Qwen/Qwen3-0.6B --max-steps 100 --max-length 4096
```

The game is fully observable. Numeric Metta RL and PufferLib training need a
bounded codec for this game's directive fields.
