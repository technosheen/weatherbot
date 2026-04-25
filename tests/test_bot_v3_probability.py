import math

import bot_v3


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def test_bucket_prob_uses_continuous_probability_for_fahrenheit_ranges():
    prob = bot_v3.bucket_prob(forecast=79.0, t_low=78.0, t_high=79.0, sigma=2.0)

    expected = _cdf((79.0 - 79.0) / 2.0) - _cdf((78.0 - 79.0) / 2.0)
    assert prob == round(expected, 4)
    assert 0.0 < prob < 1.0


def test_bucket_prob_uses_half_degree_window_for_single_celsius_buckets():
    prob = bot_v3.bucket_prob(forecast=30.0, t_low=30.0, t_high=30.0, sigma=1.2)

    expected = _cdf((30.5 - 30.0) / 1.2) - _cdf((29.5 - 30.0) / 1.2)
    assert prob == round(expected, 4)
    assert 0.0 < prob < 1.0


def test_bucket_prob_expands_edge_buckets_by_half_degree():
    below = bot_v3.bucket_prob(forecast=30.0, t_low=-999.0, t_high=29.0, sigma=1.2)
    above = bot_v3.bucket_prob(forecast=30.0, t_low=31.0, t_high=999.0, sigma=1.2)

    expected = round(_cdf((29.5 - 30.0) / 1.2), 4)
    assert below == expected
    assert above == expected
