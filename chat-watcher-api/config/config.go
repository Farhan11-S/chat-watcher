package config

import (
	"fmt"
	"os"

	"github.com/joho/godotenv"
)

type Config struct {
	DBHost      string
	DBPort      string
	DBName      string
	DBUser      string
	DBPassword  string
	Port        string
	ClipperPath string
	PythonPath  string
	BotPath     string

	// Summarizer (LLM)
	SummarizerEndpoint string
	SummarizerAPIKey   string
	SummarizerModel    string
}

func Load() (*Config, error) {
	// Load .env — ignore error if file doesn't exist (env vars may be set externally)
	_ = godotenv.Load()

	cfg := &Config{
		DBHost:      getEnv("DB_HOST", "localhost"),
		DBPort:      getEnv("DB_PORT", "5432"),
		DBName:      getEnv("DB_NAME", "chatwatcher"),
		DBUser:      getEnv("DB_USER", "chatwatcher"),
		DBPassword:  getEnv("DB_PASSWORD", ""),
		Port:        getEnv("PORT", "3000"),
		ClipperPath: getEnv("CLIPPER_PATH", "/root/codes/chat-watcher-clipper/main.py"),
		PythonPath:  getEnv("PYTHON_PATH", "/usr/local/lib/hermes-agent/venv/bin/python"),
		BotPath:           getEnv("BOT_PATH", "/root/codes/chat-watcher-bot/main.py"),
		SummarizerEndpoint: getEnv("SUMMARIZER_ENDPOINT", "https://api.openai.com/v1/chat/completions"),
		SummarizerAPIKey:   getEnv("SUMMARIZER_API_KEY", ""),
		SummarizerModel:    getEnv("SUMMARIZER_MODEL", "gpt-4o-mini"),
	}

	return cfg, nil
}

func (c *Config) DSN() string {
	return fmt.Sprintf(
		"host=%s port=%s dbname=%s user=%s password=%s sslmode=disable",
		c.DBHost, c.DBPort, c.DBName, c.DBUser, c.DBPassword,
	)
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
