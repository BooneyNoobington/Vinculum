SELECT recorded_at AS timestamp, measurement AS value
FROM samples
WHERE series_id = :series_id
  AND recorded_at >= :start
  AND recorded_at < :end
ORDER BY recorded_at;
