package handlers

import (
	"fmt"
	"regexp"
)

var videoIDPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})`),
	regexp.MustCompile(`^([A-Za-z0-9_-]{11})$`),
}

func extractVideoID(urlOrID string) (string, error) {
	for _, pattern := range videoIDPatterns {
		if matches := pattern.FindStringSubmatch(urlOrID); len(matches) > 1 {
			return matches[1], nil
		}
	}
	return "", fmt.Errorf("could not extract video ID from: %s", urlOrID)
}
