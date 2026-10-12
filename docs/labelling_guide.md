# Labelling guide: NETRAX own data (v3, 11 Oct 2026)

**The one rule that matters most: label what is visible, consistently, on every frame.**
Pre-labels in the Roboflow zips are a draft from our models: correct them, never trust them.

## PPE classes (ppe_to_label.zip, order fixed: 0 head ... 8 foot)
| Class | Draw a box when | Do NOT box |
|---|---|---|
| head | EVERY visible head, with or without helmet (box the whole head incl. the helmet) - same as the SH17 data the model learned from | back-of-head that is mostly hidden |
| helmet | a hard hat is **on a head**; box the helmet shell | helmet in the hand / on a table, caps, hoods |
| face | a face is visible (front or side, eyes area visible) - with or without goggles | back of the head |
| goggles | safety goggles / glasses **on the face**; box the goggles only | goggles on the forehead or in the hand |
| mask | a mask on the face | mask under the chin / in the hand |
| vest | a hi-vis vest **worn**; one box around the visible vest | vest on the arm / chair, plain yellow or orange T-shirts |
| hand | a **bare** hand (no glove) | a gloved hand (box as gloves only) |
| gloves | a glove **on a hand**; one box per hand | gloves lying somewhere |
| foot | every visible foot with its shoe (normal shoe, safety shoe, sandal); one box per foot | feet hidden or cut off by the frame edge |

- hand and gloves never both on the same hand (a gloved hand = gloves only).
- head + helmet DO go together on a helmeted head: head box around the head, helmet box around the shell.
- face and goggles DO go together: face box around the face, goggles box around the goggles.
- Partly hidden: box it if about 30 % or more is visible and you are sure. Unsure -> leave it out.
- Skip anything smaller than about 10 x 10 px. Fit boxes tightly.
- No person class here (person comes from the pose model).

## Fire classes (fire_to_label.zip)
| Class | Box | Do NOT box |
|---|---|---|
| Fire | every visible flame incl. its white core (candle, lighter, paper, agarbatti tip if it flames) | glowing embers without flame, screens showing fire, lamps, red/orange cloth |
| Smoke | the visible plume, incl. thin agarbatti smoke if you can see its edges | kettle steam, haze over the whole room, hair, grey walls |

The model currently MISSES most candle / paper / agarbatti flames, so most fire boxes must be DRAWN, not corrected.
Negative videos (neg_*) are not in the zip: they get empty labels automatically.

---
## Previous version (v2 model, 6 classes) - kept for reference

Written for everyone on the team who draws or corrects boxes.
**The one rule that matters most: label what is visible and worn, consistently.**

## Classes (the order is fixed and must match the models)
| PPE model id | class | Fire model id | class |
|---|---|---|---|
| 0 | person | 0 | Fire |
| 1 | helmet | 1 | Smoke |
| 2 | vest | | |
| 3 | gloves | | |
| 4 | boots (= any protective / safety shoe) | | |
| 5 | mask (skip unless someone wears one) | | |

## PPE rules
| Item | Draw a box when | Do NOT box |
|---|---|---|
| person | every person with more than about 1/3 of the body visible, including partly hidden people | reflections, posters, mannequins, clothes on a hanger |
| helmet | a hard hat is **on the head**; box only the helmet shell | a helmet in the hand or on a table; caps; hoods; bike helmets (unless we decide to count them) |
| vest | a hi-vis vest is **worn** on the torso; one box around the visible vest | a vest over the arm or on a chair; plain orange/yellow T-shirts |
| gloves | a glove **on a hand**; one box per hand | bare hands; gloves lying somewhere |
| boots | a safety shoe **on a foot**; one box per foot, even if two feet are close together | sandals, slippers, bare feet, shoes that are not being worn |

- Worn but partly hidden: still box it if you can see about 30 % or more and are sure what it is. If you are unsure, leave it out.
- Tiny: skip anything smaller than about 10 × 10 px.
- One box per object, fitted tightly. Don't include the background.
- "Carried, not worn" images (helmet in hand, vest over the arm) are **valuable negatives**: box the person, but not the item.

## Fire / smoke rules
| Class | Box | Do NOT box |
|---|---|---|
| Fire | the visible flame including its white core (match, lighter, candle, paper fire) | glowing embers without flame, phone/TV screens showing fire (keep those images as negatives with NO box), sunsets, red lamps |
| Smoke | the visible plume, including thin agarbatti smoke if you can see its edges | steam from a kettle (negative), haze over the whole room, hair, grey walls |

- For a phone showing a fire video: **no Fire box**. This teaches the model a screen is not fire. The camera may still be fooled; the IR sensor in the hardware phase is the real fix.
- Keep images with **nothing** in them (a room, a person in a red shirt, a lamp). An empty label file = a negative image, and negatives matter as much as positives.

## Workflow
1. Record: `python -m tools.collect --task ppe --scene <scene_name>` (or `--task fire`). One scene = one recording session in one place and light, e.g. `lab_fullkit_day`, `room_match_night`.
2. Upload `datasets/own/<task>/<scene>/` (images + labels) to Roboflow or CVAT as **YOLO** format. Pre-labels load automatically.
3. Correct every box, delete wrong ones, add missing ones. Pre-labels are a draft only.
4. Export in YOLO format back into the **same** scene folder, replacing `labels/`.
5. Write which scenes are for training, validation and the hard test set in `datasets/splits.yaml`.
6. `python -m tools.build_dataset --task ppe` produces `datasets/build/ppe/` and a zip for Colab.

## Quality check before training
- Two people label the same 20 images. If their boxes differ a lot, fix the guide first.
- Look at 10 random labelled images from each scene before exporting.
