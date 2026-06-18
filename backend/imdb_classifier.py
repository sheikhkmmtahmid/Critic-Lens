import os
import json
import time
import re
import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
README_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md")

TFIDF_PATH      = os.path.join(MODELS_DIR, "tfidf_vectorizer.joblib")
SGD_PATH        = os.path.join(MODELS_DIR, "sgd_classifier.joblib")
DISTILBERT_PATH = os.path.join(MODELS_DIR, "distilbert-imdb")
ACCURACY_PATH   = os.path.join(MODELS_DIR, "accuracy_report.json")
BUFFER_PATH     = os.path.join(MODELS_DIR, "incremental_buffer.json")

# how many new labelled samples to accumulate before re-fine-tuning distilbert
DISTILBERT_RETRAIN_THRESHOLD = 20

_tfidf_vec    = None
_sgd_clf      = None
_distilbert_pipe = None


# device detection
def _get_device() -> int:
    import torch
    if torch.cuda.is_available():
        logger.info(f"GPU detected: {torch.cuda.get_device_name(0)} - using CUDA")
        return 0
    logger.info("No GPU detected - using CPU")
    return -1


# data loading
def _load_imdb_data():
    path = os.path.join(DATA_DIR, "IMDB Dataset.csv")
    df = pd.read_csv(path)
    df["label"] = (df["sentiment"].str.strip().str.lower() == "positive").astype(int)
    logger.info(f"Loaded {len(df)} IMDB reviews ({df['label'].sum()} pos / {(df['label']==0).sum()} neg)")
    return df


# TF-IDF + SGDClassifier
def _train_tfidf(df):
    global _tfidf_vec, _sgd_clf

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import SGDClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score
    import joblib

    logger.info("Training TF-IDF + SGD classifier (80/10/10 split)...")
    t0 = time.time()

    X = df["review"].tolist()
    y = df["label"].tolist()

    # 80/10/10 split
    X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    X_val,   X_test, y_val,  y_test = train_test_split(X_tmp, y_tmp, test_size=0.5, random_state=42, stratify=y_tmp)

    logger.info(f"Split sizes - train: {len(X_train)}, val: {len(X_val)}, test: {len(X_test)}")

    # bigrams + sublinear TF - gives a solid accuracy bump
    _tfidf_vec = TfidfVectorizer(max_features=60000, ngram_range=(1, 2), sublinear_tf=True)
    X_train_vec = _tfidf_vec.fit_transform(X_train)
    X_val_vec   = _tfidf_vec.transform(X_val)
    X_test_vec  = _tfidf_vec.transform(X_test)

    # SGDClassifier with log_loss = logistic regression but supports partial_fit for incremental updates
    _sgd_clf = SGDClassifier(loss="log_loss", max_iter=100, random_state=42, n_jobs=-1)
    _sgd_clf.fit(X_train_vec, y_train)

    train_acc = accuracy_score(y_train, _sgd_clf.predict(X_train_vec))
    val_acc   = accuracy_score(y_val,   _sgd_clf.predict(X_val_vec))
    test_acc  = accuracy_score(y_test,  _sgd_clf.predict(X_test_vec))

    logger.info(f"TF-IDF+SGD | train: {train_acc:.4f} | val: {val_acc:.4f} | test: {test_acc:.4f} | time: {time.time()-t0:.1f}s")

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(_tfidf_vec, TFIDF_PATH)
    joblib.dump(_sgd_clf,   SGD_PATH)

    return {
        "train_accuracy": round(train_acc, 4),
        "val_accuracy":   round(val_acc,   4),
        "test_accuracy":  round(test_acc,  4),
        "train_samples":  len(X_train),
        "val_samples":    len(X_val),
        "test_samples":   len(X_test),
    }


def _load_tfidf():
    global _tfidf_vec, _sgd_clf
    import joblib
    _tfidf_vec = joblib.load(TFIDF_PATH)
    _sgd_clf   = joblib.load(SGD_PATH)
    logger.info("TF-IDF + SGD loaded from disk")


# DistilBERT fine-tuning
def _finetune_distilbert(df, device: int) -> dict:
    import torch
    from torch.utils.data import Dataset as TorchDataset
    from transformers import (
        AutoTokenizer,
        AutoModelForSequenceClassification,
        TrainingArguments,
        Trainer,
    )
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score

    logger.info("Fine-tuning DistilBERT on IMDB data (80/10/10 split, 25k sample)...")
    logger.info("This only runs once - model is saved to disk after.")

    base_model = "distilbert-base-uncased"
    tokenizer  = AutoTokenizer.from_pretrained(base_model)

    # 25k gives ~93% accuracy and trains in reasonable time on CPU/GPU
    sample = df.sample(n=min(25000, len(df)), random_state=42)
    texts  = sample["review"].tolist()
    labels = sample["label"].tolist()

    # 80/10/10
    X_train, X_tmp, y_train, y_tmp = train_test_split(texts, labels, test_size=0.2, random_state=42, stratify=labels)
    X_val,   X_test, y_val,  y_test = train_test_split(X_tmp, y_tmp, test_size=0.5, random_state=42, stratify=y_tmp)

    logger.info(f"DistilBERT split - train: {len(X_train)}, val: {len(X_val)}, test: {len(X_test)}")

    class ReviewDataset(TorchDataset):
        def __init__(self, texts, labels, tok, max_len=256):
            self.enc    = tok(texts, truncation=True, padding=True, max_length=max_len)
            self.labels = labels

        def __len__(self):
            return len(self.labels)

        def __getitem__(self, i):
            item = {k: torch.tensor(v[i]) for k, v in self.enc.items()}
            item["labels"] = torch.tensor(self.labels[i])
            return item

    train_ds = ReviewDataset(X_train, y_train, tokenizer)
    val_ds   = ReviewDataset(X_val,   y_val,   tokenizer)
    test_ds  = ReviewDataset(X_test,  y_test,  tokenizer)

    model = AutoModelForSequenceClassification.from_pretrained(
        base_model,
        num_labels=2,
        id2label={0: "NEGATIVE", 1: "POSITIVE"},
        label2id={"NEGATIVE": 0, "POSITIVE": 1},
    )

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = np.argmax(logits, axis=-1)
        return {"accuracy": float(accuracy_score(labels, preds))}

    use_cpu = (device == -1)

    args = TrainingArguments(
        output_dir=DISTILBERT_PATH,
        num_train_epochs=2,
        per_device_train_batch_size=32 if not use_cpu else 16,
        per_device_eval_batch_size=64  if not use_cpu else 32,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        logging_steps=100,
        use_cpu=use_cpu,
        report_to="none",
        learning_rate=2e-5,
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_metrics,
    )

    t0 = time.time()
    train_result = trainer.train()
    trainer.save_model(DISTILBERT_PATH)
    tokenizer.save_pretrained(DISTILBERT_PATH)

    # evaluate on the held-out test set
    test_result = trainer.predict(test_ds)
    test_preds  = np.argmax(test_result.predictions, axis=-1)
    test_acc    = float(accuracy_score(y_test, test_preds))

    # pull val accuracy from trainer log history
    eval_logs = [h for h in trainer.state.log_history if "eval_accuracy" in h]
    val_acc   = max(h["eval_accuracy"] for h in eval_logs) if eval_logs else 0.0

    elapsed = (time.time() - t0) / 60
    logger.info(f"DistilBERT done in {elapsed:.1f} min | val: {val_acc:.4f} | test: {test_acc:.4f}")

    return {
        "val_accuracy":   round(val_acc,  4),
        "test_accuracy":  round(test_acc, 4),
        "train_samples":  len(X_train),
        "val_samples":    len(X_val),
        "test_samples":   len(X_test),
        "epochs":         2,
        "base_model":     base_model,
    }


def _load_distilbert(device: int):
    global _distilbert_pipe
    from transformers import pipeline
    logger.info("Loading fine-tuned DistilBERT from disk...")
    _distilbert_pipe = pipeline("text-classification", model=DISTILBERT_PATH, device=device)
    logger.info("DistilBERT ready")


# accuracy report & README update
def _save_accuracy_report(tfidf_metrics: dict, distilbert_metrics: dict):
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "dataset": "IMDB 50k Movie Reviews",
        "split": {"train": 0.8, "val": 0.1, "test": 0.1},
        "tfidf_sgd": tfidf_metrics,
        "distilbert": distilbert_metrics,
    }
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(ACCURACY_PATH, "w") as f:
        json.dump(report, f, indent=2)
    logger.info(f"Accuracy report saved to {ACCURACY_PATH}")
    return report


def _update_readme(report: dict):
    if not os.path.exists(README_PATH):
        return

    t = report["tfidf_sgd"]
    d = report["distilbert"]

    table = f"""<!-- METRICS_START -->
## Model Performance

Both classifiers are trained on the IMDB 50k reviews dataset with an 80/10/10 train/val/test split.
Metrics are generated after every training run and saved to `models/accuracy_report.json`.

| Model | Train Acc | Val Acc | Test Acc | Samples (train) |
|---|---|---|---|---|
| TF-IDF + SGD Classifier | {t['train_accuracy']:.2%} | {t['val_accuracy']:.2%} | {t['test_accuracy']:.2%} | {t['train_samples']:,} |
| DistilBERT (fine-tuned) | - | {d['val_accuracy']:.2%} | {d['test_accuracy']:.2%} | {d['train_samples']:,} |

_Last trained: {report['generated_at'][:10]}_
<!-- METRICS_END -->"""

    with open(README_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # replace between markers if they exist, otherwise append
    pattern = r"<!-- METRICS_START -->.*?<!-- METRICS_END -->"
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, table, content, flags=re.DOTALL)
    else:
        content = content.rstrip() + "\n\n" + table + "\n"

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("README.md updated with accuracy metrics")


# incremental learning
def _load_buffer() -> list:
    if not os.path.exists(BUFFER_PATH):
        return []
    with open(BUFFER_PATH, "r") as f:
        return json.load(f).get("samples", [])


def _save_buffer(samples: list):
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(BUFFER_PATH, "w") as f:
        json.dump({"samples": samples}, f)


def _incremental_distilbert(new_texts: list, new_labels: list, device: int):
    #Continued fine-tuning with replay from the original IMDB data to prevent catastrophic forgetting. 
    # #This is a simple and effective strategy for incremental updates on small datasets. 
    # #We mix the new samples with a random subset of the original training data (5x the new sample count, capped at 500) to keep the model grounded 
    # #in its original knowledge while learning from new examples. The updated model is saved to disk and reloaded into the pipeline for inference.
    import torch
    from torch.utils.data import Dataset as TorchDataset
    from transformers import (
        AutoTokenizer,
        AutoModelForSequenceClassification,
        TrainingArguments,
        Trainer,
    )

    logger.info(f"Incremental DistilBERT fine-tuning on {len(new_texts)} new samples + replay...")

    tokenizer = AutoTokenizer.from_pretrained(DISTILBERT_PATH)

    # replay: mix original IMDB samples to prevent catastrophic forgetting
    # using 5x the new sample count from IMDB keeps the old knowledge grounded
    replay_n = min(len(new_texts) * 5, 500)
    orig_df  = _load_imdb_data().sample(n=replay_n, random_state=42)
    replay_texts  = orig_df["review"].tolist()
    replay_labels = orig_df["label"].tolist()

    all_texts  = new_texts  + replay_texts
    all_labels = new_labels + replay_labels

    class ReviewDataset(TorchDataset):
        def __init__(self, texts, labels, tok):
            self.enc    = tok(texts, truncation=True, padding=True, max_length=256)
            self.labels = labels

        def __len__(self):
            return len(self.labels)

        def __getitem__(self, i):
            item = {k: torch.tensor(v[i]) for k, v in self.enc.items()}
            item["labels"] = torch.tensor(self.labels[i])
            return item

    dataset = ReviewDataset(all_texts, all_labels, tokenizer)

    model = AutoModelForSequenceClassification.from_pretrained(DISTILBERT_PATH)

    use_cpu = (device == -1)
    args = TrainingArguments(
        output_dir=DISTILBERT_PATH,
        num_train_epochs=1,
        per_device_train_batch_size=16 if not use_cpu else 8,
        # very low lr to avoid overwriting what the model already knows
        learning_rate=1e-5,
        logging_steps=50,
        save_strategy="no",
        use_cpu=use_cpu,
        report_to="none",
    )

    trainer = Trainer(model=model, args=args, train_dataset=dataset)
    trainer.train()
    trainer.save_model(DISTILBERT_PATH)
    tokenizer.save_pretrained(DISTILBERT_PATH)

    logger.info("Incremental DistilBERT update saved")

    # reload the pipeline with the updated model
    _load_distilbert(device)


def incremental_update(text: str, sentiment: str) -> dict:
    # Add a new labelled review and update both models.
    # sentiment should be 'positive' or 'negative'.
    global _sgd_clf, _tfidf_vec

    if _tfidf_vec is None or _sgd_clf is None:
        raise RuntimeError("Models not loaded")

    label = 1 if sentiment.strip().lower() == "positive" else 0

    # update TF-IDF + SGD immediately - partial_fit is instant
    import joblib
    clipped = " ".join(text.split()[:500])
    vec = _tfidf_vec.transform([clipped])
    _sgd_clf.partial_fit(vec, [label])
    joblib.dump(_sgd_clf, SGD_PATH)

    # buffer this sample for DistilBERT
    buffer = _load_buffer()
    buffer.append({"text": text, "label": label, "timestamp": datetime.utcnow().isoformat()})
    _save_buffer(buffer)

    distilbert_updated = False
    if len(buffer) >= DISTILBERT_RETRAIN_THRESHOLD:
        device = _get_device()
        texts  = [s["text"]  for s in buffer]
        labels = [s["label"] for s in buffer]
        _incremental_distilbert(texts, labels, device)
        _save_buffer([])  # clear buffer after training
        distilbert_updated = True
        logger.info("DistilBERT incrementally updated and buffer cleared")

    return {
        "tfidf_updated":      True,
        "distilbert_updated": distilbert_updated,
        "buffer_size":        0 if distilbert_updated else len(buffer),
        "buffer_threshold":   DISTILBERT_RETRAIN_THRESHOLD,
    }


# public entry point
def load_models():
    device = _get_device()

    tfidf_ready      = os.path.exists(TFIDF_PATH) and os.path.exists(SGD_PATH)
    distilbert_ready = os.path.exists(os.path.join(DISTILBERT_PATH, "config.json"))

    df = None if (tfidf_ready and distilbert_ready) else _load_imdb_data()

    tfidf_metrics      = None
    distilbert_metrics = None

    if tfidf_ready:
        _load_tfidf()
    else:
        tfidf_metrics = _train_tfidf(df)

    if distilbert_ready:
        _load_distilbert(device)
    else:
        distilbert_metrics = _finetune_distilbert(df, device)
        _load_distilbert(device)

    if tfidf_metrics and distilbert_metrics:
        report = _save_accuracy_report(tfidf_metrics, distilbert_metrics)
        _update_readme(report)
    elif os.path.exists(ACCURACY_PATH):
        logger.info("Loaded existing accuracy report from disk")

    logger.info("Both IMDB classifiers ready")


# inference
def is_ready() -> bool:
    return _tfidf_vec is not None and _distilbert_pipe is not None


def is_tfidf_ready() -> bool:
    return _tfidf_vec is not None and _sgd_clf is not None


def is_distilbert_ready() -> bool:
    return _distilbert_pipe is not None


def get_accuracy_report() -> dict:
    if os.path.exists(ACCURACY_PATH):
        with open(ACCURACY_PATH) as f:
            return json.load(f)
    return {}


def predict_fast(text: str) -> dict:
    if not is_tfidf_ready():
        raise RuntimeError("TF-IDF model not loaded")
    clipped = " ".join(text.split()[:500])
    vec     = _tfidf_vec.transform([clipped])
    proba   = _sgd_clf.predict_proba(vec)[0]
    label   = "Positive" if proba[1] >= proba[0] else "Negative"
    return {"sentiment": label, "confidence": round(float(max(proba)), 4), "model": "tfidf_sgd"}


def predict_deep(text: str) -> dict:
    if not is_distilbert_ready():
        raise RuntimeError("DistilBERT not loaded")
    clipped = " ".join(text.split()[:300])
    result  = _distilbert_pipe(clipped, truncation=True, max_length=512)[0]
    label_map = {"POSITIVE": "Positive", "NEGATIVE": "Negative"}
    label     = label_map.get(result["label"], result["label"])
    return {"sentiment": label, "confidence": round(float(result["score"]), 4), "model": "distilbert"}


def predict_ensemble(text: str) -> dict:
    fast = predict_fast(text)
    deep = predict_deep(text)

    if fast["sentiment"] == deep["sentiment"]:
        return {
            "sentiment":  fast["sentiment"],
            "confidence": round((fast["confidence"] + deep["confidence"]) / 2, 4),
            "agreement":  True,
            "model":      "ensemble",
            "tfidf":      fast,
            "distilbert": deep,
        }
    else:
        # they disagree - trust distilbert but flag it
        return {
            "sentiment":  deep["sentiment"],
            "confidence": round(deep["confidence"] * 0.82, 4),
            "agreement":  False,
            "model":      "ensemble_disagreement",
            "tfidf":      fast,
            "distilbert": deep,
        }
