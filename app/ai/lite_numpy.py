"""Printer Buddy AI - "lite" model runner: plain numpy, no PyTorch / ONNX.

Made for small boxes like a Creality K1 (32-bit MIPS, Python 3.8, numpy 1.16), where
onnxruntime and PyTorch don't exist. Runs the MobileNetV3-Small that export.py --kind lite
writes as one .npz file (weights + a "__spec__" layer list; batch-norm already folded in).

  m = LiteModel.load("printer-buddy-lite.npz")
  scores = m.score_image(open("frame.jpg", "rb").read())   # [0-1 per label], same order as model.json

Only uses numpy features that exist in numpy 1.16.
"""
import io
import json

import numpy as np


def hswish(x):
    return x * np.clip(x + 3.0, 0.0, 6.0) / 6.0


def hsigmoid(x):
    return np.clip(x + 3.0, 0.0, 6.0) / 6.0


def relu(x):
    return np.maximum(x, 0.0)


ACTS = {"none": lambda x: x, "relu": relu, "hswish": hswish, "hsigmoid": hsigmoid}


def conv2d(x, w, b, stride, pad, groups):
    """x [C,H,W], w [O,C/groups,k,k], b [O] -> [O,Ho,Wo]. groups is 1 or C (depthwise)."""
    c, h, wd = x.shape
    o, cg, k, _ = w.shape
    if pad:
        x = np.pad(x, ((0, 0), (pad, pad), (pad, pad)), mode="constant")
    ho = (h + 2 * pad - k) // stride + 1
    wo = (wd + 2 * pad - k) // stride + 1
    if groups == 1 and k == 1:                       # pointwise: one matrix multiply
        if stride > 1:
            x = x[:, ::stride, ::stride]
        out = w.reshape(o, c).dot(x[:, :ho, :wo].reshape(c, ho * wo))
    elif groups == 1:                                # normal conv: patches, then one multiply
        s0, s1, s2 = x.strides
        cols = np.lib.stride_tricks.as_strided(
            x, shape=(c, k, k, ho, wo), strides=(s0, s1, s2, s1 * stride, s2 * stride))
        out = w.reshape(o, c * k * k).dot(cols.reshape(c * k * k, ho * wo))
    elif groups == c == o and cg == 1:               # depthwise: k*k shifted multiply-adds
        out = np.zeros((c, ho, wo), dtype=np.float32)
        for i in range(k):
            for j in range(k):
                out += w[:, 0, i, j][:, None, None] * x[:, i:i + stride * ho:stride, j:j + stride * wo:stride]
        out = out.reshape(o, ho * wo)
    else:
        raise ValueError("unsupported conv groups=%d" % groups)
    return (out + b[:, None]).reshape(o, ho, wo)


class LiteModel:
    def __init__(self, spec, arrays):
        self.spec = spec
        self.a = {k: np.asarray(v, dtype=np.float32) for k, v in arrays.items()}
        self.size = int(spec["image_size"])
        self.mean = np.array(spec["mean"], dtype=np.float32)[:, None, None]
        self.std = np.array(spec["std"], dtype=np.float32)[:, None, None]

    @classmethod
    def load(cls, path):
        z = np.load(path)
        spec = json.loads(bytes(z["__spec__"]).decode("utf-8"))
        return cls(spec, {k: z[k] for k in z.files if k != "__spec__"})

    def forward(self, x):
        """x: float32 [3,S,S], already normalized. Returns raw scores (before sigmoid)."""
        a, stack = self.a, []
        for L in self.spec["layers"]:
            op = L["op"]
            if op == "conv":
                x = ACTS[L["act"]](conv2d(x, a[L["w"]], a[L["b"]], L["stride"], L["pad"], L["groups"]))
            elif op == "se":
                s = x.mean(axis=(1, 2))
                s = relu(a[L["w1"]].dot(s) + a[L["b1"]])
                s = hsigmoid(a[L["w2"]].dot(s) + a[L["b2"]])
                x = x * s[:, None, None]
            elif op == "res_begin":
                stack.append(x)
            elif op == "res_end":
                x = x + stack.pop()
            elif op == "gap":
                x = x.mean(axis=(1, 2))
            elif op == "linear":
                x = ACTS[L["act"]](a[L["w"]].dot(x) + a[L["b"]])
            else:
                raise ValueError("unknown layer " + op)
        return x

    def preprocess(self, img):
        """PIL image -> normalized [3,S,S] float32 (same as training: plain resize, RGB, 0-1, mean/std)."""
        from PIL import Image
        img = img.convert("RGB").resize((self.size, self.size), Image.BILINEAR)
        x = np.asarray(img, dtype=np.float32).transpose(2, 0, 1) / 255.0
        return (x - self.mean) / self.std

    def score(self, x):
        z = self.forward(x.astype(np.float32))
        return 1.0 / (1.0 + np.exp(-z))

    def score_image(self, data):
        """JPEG/PNG bytes -> list of 0-1 scores, one per label."""
        from PIL import Image
        return [float(v) for v in self.score(self.preprocess(Image.open(io.BytesIO(data))))]
