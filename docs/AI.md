# Printer Buddy AI - failure detection

> **Status:** live. Pick **AI detection: basic** in `./install.sh` (needs a 64-bit system) and the installer
> downloads the newest model by itself. **3) Update** fetches newer models.

Printer Buddy can watch your printer's camera and catch failed prints. While a printer is printing,
it scores each snapshot for these problems (0-100% each, several can be high at once):

| Label | Kind | What it means |
|---|---|---|
| spaghetti | **alert** | print came loose / extruding into the air |
| warping | **alert** | corners lifting off the bed |
| first_layer | **alert** | bad first layer |
| failure | **alert** | a failure the source data didn't name |
| stringing | warning | thin strings between parts |
| blob | warning | blobs, zits, under-extrusion |
| crack | warning | layer splits / cracks |

**Alert** labels can notify you and pause the print. **Warnings** only show on the card.
It only acts when a problem **holds for several checks in a row**, so one odd frame won't pause a good print.

## Download

Models are published as releases on this repo, tagged `ai-<model>`:

- **All AI models:** [Releases tagged `ai-`](https://github.com/printer-buddy/printer-buddy/releases?q=ai-&expanded=true)
- **Current:** [`ai-basic-v1`](https://github.com/printer-buddy/printer-buddy/releases/tag/ai-basic-v1)

Each release has two files:

- `printer-buddy-basic.onnx` - the model (about 17 MB)
- `model.json` - labels, image size, default threshold, test results and a sha256 checksum

The installer downloads the newest one for you when you pick **AI detection: basic**.

## How it watches

- While a printer is **printing**, it checks the camera every 10 seconds. When it isn't printing, it doesn't watch,
  and the printer page says so.
- It alerts when an alert label scores **60% or more for 3 checks in a row**, then waits
  **10 minutes** before alerting again. All of these can be changed under **AI** in the dashboard.
- Once a check scores that high, it checks **5 seconds sooner** (every 5 seconds instead of 10, never faster than every
  2 seconds) until it alerts, then goes back to every 10 seconds. So a real failure is caught in about 20 seconds
  instead of 30. It only speeds up again for a new failure, after a check has come back clean.
- Each printer has its own mode in its menu and on its page: **Alert only** (default), **Alert + auto-pause**, or **Off**.
- **Check now** on a printer's page scores the camera once, any time, without alerting or pausing. Handy for testing.
- When an alert label reaches the threshold, a **red box** on the printer page's camera (and on the alert's
  notification photo) shows where the AI thinks the problem is. The model only scores whole pictures, so the box
  comes from greying out one patch of the picture at a time (a 6 x 6 grid, 25 patches, about 2 seconds on a Pi 4) and seeing where the score
  drops most. It's a rough area, not an outline. When no one part of the picture explains the score (say the
  spaghetti fills the whole view), no box is drawn. Photos kept for the AI are saved without the box.
- Two box options in the printer page's AI watch panel, remembered per printer:
    - **Box: always**: a box on every check, the likeliest spot even on a good print (the extra runs every check).
    - **Box: on failure** (default): a box only at or above the threshold, so the extra runs only happen when a
      failure is likely.
    - With both off, the box is skipped completely: no extra runs, which helps older Pis. Alerts still work.
- The AI judges what the camera sees: point it at **the print on the bed**, not just the nozzle.

## Help improve the AI (optional, off by default)

The next model learns from real printers. Printer Buddy keeps a few labelled photos **on your box**:

- after an alert it asks **"Was it really a failure?"** (and if yes, **how bad, 1-10**)
- 3 snapshots per print (10%, 50%, 90%), which become **OK** when the print finishes
- the last snapshot of a failed or cancelled print, for you to label
- anything you save yourself with **Save for AI…** on a printer's page

You can see, label and delete all of them under **AI > Photos for AI**. **Nothing leaves your box** unless you tick
**Help improve the AI**. If you do:

- only **labelled** photos are sent, one at a time, at most 50 a day
- each is shrunk to 640 px and re-saved, so no location or camera details
- sent with your **box number** (the random ID in your share links), the label, how bad it was, and what the AI scored
  at the time; **not** your name, address or printer list (your internet address is only used to count the daily
  limit for 2 days and is never stored with the photo)
- they go to a **private** dataset used only to train Printer Buddy's AI models
- once sent, the photo is deleted from your box; untick the box any time and nothing more is sent

Photos show whatever your printer camera sees, so keep people and private things out of its view.

## Was it right?

Every labelled photo records whether the AI got it right: **right**, **false alarm** (it flagged a problem, you said
OK) or **missed** (it said OK, you labelled a failure). **AI > Photos for AI** shows the score on your printers,
e.g. *basic-v1 on your printers: 47 right · 2 false alarms · 1 missed (94% right)*.

## Current model: `basic-v1`

- MobileNetV3-Large, 224 x 224 input, ONNX, runs on a CPU (about 7 ms per image on a PC; a Pi 4 is slower but fine for a check every few seconds)
- Trained on 31,934 images, tested on 2,907 held-out images
- Default alert threshold **0.6**

Test results (alert labels, per frame, before the "hold for several checks" rule):

| Threshold | Failures caught | Correct when it alarms | False alarms on frames without an alert problem |
|---|---|---|---|
| 0.5 | 92.2% | 95.7% | 4.0% |
| **0.6** | **91.5%** | **96.2%** | **3.5%** |
| 0.7 | 90.6% | 96.5% | 3.2% |
| 0.8 | 89.3% | 97.0% | 2.6% |

### Limits (read this)

- The test images come from the **same kinds of sources** as the training images. On your own
  printers, cameras and lighting it **will do worse** than the table above. Tell us about misses and false alarms.
- **first_layer** looks perfect in testing but most of its images come from one dataset, so it may be
  recognising that camera rather than the defect. Don't trust it yet.
- **failure** is a vague label from datasets that only said "fail"; it scores poorly on its own.
  Real failures are usually caught by spaghetti or warping anyway.
- Clean "OK" prints are only about 11% of the data, so false alarms are the main thing we're working on.

## How it was made

1. Twelve public datasets were downloaded, their labels mapped onto ours, and duplicates removed
   (4,766 copies) to make one dataset: 34,841 images.
2. Every image was resized (max 640 px) and given image-level labels. Box sets: any box = that label.
   Folder sets: the folder name is the label. Images with no problem are "OK".
3. A MobileNetV3-Large (starting from ImageNet weights) was trained for 30 epochs in Google Colab;
   the best epoch (24) was kept and exported to ONNX.

## Credits

### Datasets

Thank you to everyone who published these. We **resized, relabelled and merged** the images;
nothing here is endorsed by the original authors.

| Dataset | Author | License | Images used |
|---|---|---|---|
| [3D printing defects](https://universe.roboflow.com/hnnm/3d-printing-defects-eavhn) | hnnm (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 16,133 |
| [3D printing error](https://universe.roboflow.com/cameteam/3d-printing-error-cv8ue) | cameteam (Roboflow) | MIT | 4,274 |
| [3DPrintFail](https://universe.roboflow.com/minedorothy/3dprintfail) | minedorothy (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 4,347 |
| [3D print fail detection](https://universe.roboflow.com/detection-y0hrz/3d-print-fail-detection) | detection-y0hrz (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 2,058 |
| [3Dprint3](https://universe.roboflow.com/edison-jyxbh/3dprint3) | edison-jyxbh (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 1,372 |
| [3d-ho5at](https://universe.roboflow.com/automatisierung-projekt-aajut/3d-ho5at) | automatisierung-projekt-aajut (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 3,493 |
| [FDM defects](https://universe.roboflow.com/defects-of-3d-printing/fdm-sw9eb) | defects-of-3d-printing (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | 3,888 |
| [3dprinting](https://universe.roboflow.com/additive-manufacturing-qa/3dprinting) | additive-manufacturing-qa (Roboflow) | Public Domain | 692 |
| [3D printing errors](https://www.kaggle.com/datasets/mikulhe/3d-printing-errors) | mikulhe (Kaggle) | see dataset page | 388 |
| [3D printing success/failure (finetuned)](https://www.kaggle.com/datasets/bshaurya/3d-printing-success-failure-dataset-finetuned) | bshaurya (Kaggle) | see dataset page | 327 |
| [Defect 3D printing](https://www.kaggle.com/datasets/muhammadmeftahmafazy/defect-3d-printing) | muhammadmeftahmafazy (Kaggle) | see dataset page | 635 |
| [3D printing errors](https://www.kaggle.com/datasets/nimbus200/3d-printing-errors) | nimbus200 (Kaggle) | see dataset page | 2,000 (of 17,883) |

"Images used" is before duplicates were removed. Several sets share images, so the final dataset is smaller.

### Added for the next model (`basic-v2`)

| Dataset | Author | License |
|---|---|---|
| [3D FDM faults detection](https://universe.roboflow.com/wangdada/3d_fdm_faults_detection-krkkr) | wangdada (Roboflow) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [3Dprinting](https://universe.roboflow.com/hnnm/3dprinting-32nsa) | hnnm (Roboflow) | Public Domain |
| [KS1 full dataset](https://github.com/mann1x/anycubic/releases/tag/datasets%2Fks1-full) | mann1x (Anycubic Kobra S1 camera, from BigEdge-FDM-Models) | none stated - credited here; ask the author before a model trained on it is released |

Plus photos shared by Printer Buddy users through **Help improve the AI**.

### Ideas

- [PrintGuard](https://github.com/oliverbravery/PrintGuard) by Oliver Bravery (GPL-2.0) - showed that
  a small, fast model on a Pi works, and the "hold for a while before acting" alert idea. Its list of
  datasets pointed us to several of the sets above. **No PrintGuard code or model is used.**

### Software

- [PyTorch / torchvision](https://github.com/pytorch/vision) (BSD-3-Clause) - MobileNetV3 and training
- MobileNetV3 starting weights trained on [ImageNet](https://www.image-net.org/) (ImageNet's terms are for research use; check them before any commercial use)
- [ONNX](https://github.com/onnx/onnx) and [ONNX Runtime](https://github.com/microsoft/onnxruntime) (Apache-2.0 / MIT) - running the model
- [Roboflow](https://roboflow.com) and [Kaggle](https://www.kaggle.com) - dataset hosting
