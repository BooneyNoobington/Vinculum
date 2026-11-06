SELECT series_id, recorded_at AS timestamp, measurement AS value
FROM samples
WHERE series_id IN :series_ids
ORDER BY series_id, recorded_at;
