import logging
import re

logger = logging.getLogger(__name__)

# keyword map for each aspect - helps find relevant sentences in the review
ASPECT_KEYWORDS = {
    "acting and performances": ["act", "perform", "cast", "actor", "actress", "role", "portray", "character", "star"],
    "plot and story": ["plot", "story", "narrative", "script", "storyline", "tale", "premise", "writing"],
    "pacing and editing": ["pac", "edit", "slow", "fast", "dragg", "rush", "brisk", "tight", "length", "too long"],
    "direction and filmmaking": ["direct", "film", "craft", "vision", "helmed", "helm", "auteur", "technique"],
    "visuals and cinematography": ["visual", "cinemat", "shot", "photograph", "look", "beauti", "gorgeous", "stunning", "camera"],
    "dialogue and writing": ["dialog", "dialogue", "line", "script", "writ", "speak", "verbal", "word"],
    "ending and resolution": ["end", "final", "conclus", "resolv", "finish", "last act", "climax", "third act"],
    "soundtrack and score": ["music", "score", "sound", "composit", "soundtrack", "track", "audio"],
}

# module-level reference - set during startup
_sentiment_pipeline = None


def load_model():
    global _sentiment_pipeline
    from transformers import pipeline
    import time

    logger.info("Loading nlptown/bert-base-multilingual-uncased-sentiment...")
    t0 = time.time()

    _sentiment_pipeline = pipeline(
        "sentiment-analysis",
        model="nlptown/bert-base-multilingual-uncased-sentiment",
        device=-1,
    )

    elapsed = time.time() - t0
    logger.info(f"bert sentiment loaded in {elapsed:.1f}s")
    return _sentiment_pipeline


def get_pipeline():
    return _sentiment_pipeline


def _split_sentences(text: str):
    # simple sentence splitter - good enough for reviews
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if len(s.strip()) > 10]


def _find_relevant_sentences(text: str, aspect: str):
    keywords = ASPECT_KEYWORDS.get(aspect, [])
    sentences = _split_sentences(text)
    relevant = []

    for sent in sentences:
        sent_lower = sent.lower()
        if any(kw in sent_lower for kw in keywords):
            relevant.append(sent)

    return relevant


def _map_stars_to_sentiment(stars: int) -> str:
    # straightforward star to sentiment mapping
    if stars <= 2:
        return "Negative"
    elif stars == 3:
        return "Neutral"
    else:
        return "Positive"


def score_aspect_sentiment(review_text: str, aspect: str) -> dict:
    if _sentiment_pipeline is None:
        raise RuntimeError("Sentiment pipeline not loaded yet")

    relevant_sentences = _find_relevant_sentences(review_text, aspect)

    if relevant_sentences:
        text_to_score = " ".join(relevant_sentences[:3])  # cap at 3 sentences
    else:
        # fall back to the full review if nothing matched
        words = review_text.split()
        text_to_score = " ".join(words[:300])

    result = _sentiment_pipeline(text_to_score[:512], truncation=True)[0]

    # not sure why but the star ratings come back as strings sometimes, so casting just in case
    label_str = str(result["label"])
    stars = int(label_str.split()[0])
    confidence = float(result["score"])
    sentiment = _map_stars_to_sentiment(stars)

    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "stars": stars,
    }
