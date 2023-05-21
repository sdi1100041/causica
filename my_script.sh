#!/bin/bash

# array of task names
tasks=("Ecoli1" "Ecoli2" "Yeast1" "Yeast2" "Yeast3")

# max number of parallel commands
M=5

# array to hold commands
commands=()

# loop over tasks
for task in "${tasks[@]}"
do
    # loop over N values
    for N in $(seq 5 5 43)
    do
        # construct the name
        name="${task}_${N}"

        # construct the command
        cmd="python -m causica.run_experiment $name --model_type rhino -dc configs/dataset_config_temporal_causal_dataset.json --model_config configs/rhino/model_config_rhino_ecoli1.json -dv gpu -c"

        # append the command to the commands array
        commands+=("$cmd")
    done
    # including 43
    N=43
    name="${task}_${N}"
    cmd="python -m causica.run_experiment $name --model_type rhino -dc configs/dataset_config_temporal_causal_dataset.json --model_config configs/rhino/model_config_rhino_ecoli1.json -dv gpu -c"
    commands+=("$cmd")
done

# print commands to stdout, pipe to xargs
printf '%s\n' "${commands[@]}" | xargs -I {} -P $M bash -c "{}"

