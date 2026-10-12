# datasets/ (own data, plan section 8)

```
datasets/
  own/ppe/<scene>/images/*.jpg  labels/*.txt     <- python -m tools.collect --task ppe --scene <scene>
  own/fire/<scene>/images/*.jpg labels/*.txt     <- python -m tools.collect --task fire --scene <scene>
  splits.yaml                                    <- copy splits.example.yaml, list scenes per split
  build/<task>/{train,val,test}/ + data.yaml     <- python -m tools.build_dataset --task <task> --zip
```
Labelling rules: docs/labelling_guide.md.  What to record: docs/data_plan.md.
Evaluate: python -m tools.evaluate --task ppe --data datasets/build/ppe/data.yaml --split test --name hard_v2
