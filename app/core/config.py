from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    MONGODB_URI: str = "mongodb://localhost:27017"
    DATABASE_NAME: str = "smart_waste"

    JWT_SECRET_KEY: str = "dev-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    UPLOAD_DIR: str = "uploads"
    MAX_IMAGE_SIZE_MB: int = 2

    ADMIN_EMAIL: str = "admin@smart-waste.local"
    ADMIN_PASSWORD: str = "admin-change-this"


settings = Settings()
