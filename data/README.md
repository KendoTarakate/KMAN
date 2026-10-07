# data/

Recordings go here (gitignored). Each session is `<name>.parquet` + `<name>.bin` +
`<name>.labels.jsonl` (see the main README, "Files per recording").

`sample_synthetic.parquet` is a **synthetic** 440 s session from the simulator
(`python host/sim_device.py --write-session ...`) with the bench hardware's
channels (chest LSM6DSOX + neck flex) and ground-truth labels covering every
activity. It is only for trying the tools; it is not real physiology.
