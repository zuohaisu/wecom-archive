"""Response schemas for tenant onboarding status endpoints."""

from pydantic import BaseModel


class OnboardingStatusOut(BaseModel):
    first_run: bool


class OnboardingCompleteOut(BaseModel):
    first_run: bool
