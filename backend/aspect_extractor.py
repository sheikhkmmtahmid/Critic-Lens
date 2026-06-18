import logging

logger = logging.getLogger(__name__)

# the 8 aspects we care about
ASPECT_LABELS = [
    "acting and performances",
    "plot and story",
    "pacing and editing",
    "direction and filmmaking",
    "visuals and cinematography",
    "dialogue and writing",
    "ending and resolution",
    "soundtrack and score",
]

# module-level pipeline ref - gets set during startup
_classifier = None


def load_model():
    global _classifier
    from transformers import pipeline
    import time

    logger.info("Loading facebook/bart-large-mnli for aspect extraction...")
    t0 = time.time()

    # this took forever to figure out - bart needs the text truncated or it crashes on long reviews
    _classifier = pipeline(
        "zero-shot-classification",
        model="facebook/bart-large-mnli",
        device=-1,  # cpu - change to 0 if you have a gpu
    )

    elapsed = time.time() - t0
    logger.info(f"bart-large-mnli loaded in {elapsed:.1f}s")
    return _classifier


def get_classifier():
    return _classifier


def extract_aspects(review_text: str) -> dict:
    if _classifier is None:
        raise RuntimeError("Aspect classifier not loaded yet")

    # truncate to roughly 512 tokens - simple word-based estimate is fine here
    words = review_text.split()
    if len(words) > 400:
        review_text = " ".join(words[:400])

    result = _classifier(
        review_text,
        ASPECT_LABELS,
        multi_label=True,
    )

    # zip the labels and scores together
    aspects = {}
    for label, score in zip(result["labels"], result["scores"]):
        # filter out low confidence ones - below 0.15 is basically noise
        if score > 0.15:
            aspects[label] = float(score)

    return aspects
