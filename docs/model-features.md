# Frozen decision model features

These five features are read from the shadow workspace at signal time and saved in the signal payload. No future price, outcome, or post-signal quote enters prediction.

1. `regime`: positive = 1, transition = 0, negative = -1.
2. `zero_distance_implied`: signed spot minus zero gamma, divided by the 0DTE implied move.
3. `wall_distance_implied`: absolute distance to the nearest call or put wall, divided by the 0DTE implied move.
4. `range_consumed`: session range divided by the implied move, as computed at signal time.
5. `minutes_to_close`: minutes until 16:00 Eastern at signal time.

An unavailable feature leaves the probability null. Coefficients are fit offline by scenario family. Publication requires the data gate, positive day-clustered Brier skill interval, and explicit report review; the saved artifact defaults to `user_reviewed: false`.
Timeouts count as non-target outcomes in fitting and in the displayed target rate.
