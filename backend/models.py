from pydantic import BaseModel
from typing import Optional, List, Any


class HealthResponse(BaseModel):
    status: str
    models_loaded: bool
    imdb_tfidf_ready: bool
    imdb_distilbert_ready: bool


class FilmSearchResult(BaseModel):
    movie_title: Optional[str]
    tomatometer_rating: Optional[float]
    audience_rating: Optional[float]
    divergence_score: Optional[float]
    rotten_tomatoes_link: Optional[str]


class IMDBSentiment(BaseModel):
    sentiment: str
    confidence: float
    model: str


class IMDBEnsemble(BaseModel):
    sentiment: str
    confidence: float
    agreement: bool
    model: str
    tfidf: IMDBSentiment
    distilbert: IMDBSentiment


class CriticReview(BaseModel):
    review_content: Optional[str]
    critic_name: Optional[str]
    publisher_name: Optional[str]
    review_type: Optional[str]
    review_score: Optional[str]
    sentiment_fast: Optional[IMDBSentiment] = None
    sentiment_deep: Optional[IMDBSentiment] = None


class FilmDivergenceResponse(BaseModel):
    movie_title: Optional[str]
    tomatometer_rating: Optional[float]
    audience_rating: Optional[float]
    divergence_score: Optional[float]
    divergence_label: Optional[str]
    genres: Optional[str]
    directors: Optional[str]
    original_release_date: Optional[str]
    release_year: Optional[Any]
    critics_consensus: Optional[str]
    runtime: Optional[Any]
    rotten_tomatoes_link: Optional[str]
    poster_url: Optional[str] = None
    # 'imdb' = audience score is IMDB rating × 10 proxy; None = real RT audience score
    audience_score_source: Optional[str] = None
    critic_reviews: List[CriticReview] = []


class MonthlyDataPoint(BaseModel):
    period: str
    fresh_count: int
    rotten_count: int
    fresh_ratio: Optional[float]


class TemporalDriftResponse(BaseModel):
    trend: str
    p_value: Optional[float]
    tau: Optional[float]
    monthly_data: List[MonthlyDataPoint] = []
    warning: Optional[str] = None
    review_count: Optional[int] = None


class LeaderboardFilm(BaseModel):
    movie_title: Optional[str]
    tomatometer_rating: Optional[float]
    audience_rating: Optional[float]
    divergence_score: Optional[float]
    divergence_label: Optional[str]
    genres: Optional[str]
    rotten_tomatoes_link: Optional[str]


class LeaderboardResponse(BaseModel):
    critic_favoured: List[LeaderboardFilm] = []
    audience_favoured: List[LeaderboardFilm] = []


class AspectResult(BaseModel):
    aspect: str
    confidence: float
    sentiment: str
    sentiment_confidence: float
    stars: int


class ReviewAnalysisResponse(BaseModel):
    aspects: List[AspectResult] = []
    overall_sentiment: str
    imdb_sentiment: Optional[IMDBEnsemble] = None


class ReviewAnalysisRequest(BaseModel):
    review_text: str


class IncrementalTrainRequest(BaseModel):
    review_text: str
    sentiment: str  # "positive" or "negative"


class IncrementalTrainResponse(BaseModel):
    success: bool
    message: str
    tfidf_updated: bool
    distilbert_updated: bool
    buffer_size: int
    buffer_threshold: int


class GenreDivergence(BaseModel):
    genre: str
    avg_divergence: float


class OverviewResponse(BaseModel):
    total_films: int
    avg_divergence: Optional[float]
    most_divergent_critic_film: Optional[str]
    most_divergent_audience_film: Optional[str]
    genre_divergence_averages: List[GenreDivergence] = []
