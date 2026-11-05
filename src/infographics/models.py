from __future__ import annotations

from pydantic import BaseModel, Field


class ScenarioParagraph(BaseModel):
    number: int
    text: str


class PersonalityMention(BaseModel):
    paragraph_number: int
    name: str
    query: str
    category: str = "people"
    # A given subject (order of the reels brain): its card is named by this key, not by the image number.
    key: str | None = None


class PersonalityInfo(BaseModel):
    name: str
    years: str | None = None


class ImageCandidate(BaseModel):
    source_id: str
    title: str
    url: str


class PersonalityImage(BaseModel):
    file: str
    source_id: str
    url: str


class PersonalityEntry(BaseModel):
    paragraph_number: int
    paragraph: str
    personality: PersonalityInfo
    # Which niche this subject belongs to (see scenario/categories.py):
    # people, places (battles, institutions, landmarks) or artifacts
    # (families, arms, books, laws, monuments).
    category: str = "people"
    # The detector's disambiguated search query ("Godwin, Earl of Wessex"),
    # kept so a later --fill-missing pass searches for the right person
    # instead of the bare name.
    query: str | None = None
    images: list[PersonalityImage] = Field(default_factory=list)
    # Given subjects only: the caller's id of the card (the reels brain uses its shot id, e.g. "12.3").
    key: str | None = None
