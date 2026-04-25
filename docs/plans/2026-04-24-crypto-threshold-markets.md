# Crypto Threshold Markets Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a separate paper-trading path for Polymarket crypto threshold markets without changing the current weather bot behavior.

**Architecture:** Keep `bot_v2.py` untouched. Build a parallel `crypto_bot.py` entrypoint plus a small `crypto/` package containing market models, Polymarket discovery, spot-price fetching, and a threshold-market strategy. The first phase is read-only scanning and paper signal generation for BTC/ETH threshold markets.

**Tech Stack:** Python stdlib, `requests`, Polymarket Gamma/CLOB public APIs, Coinbase spot price API, `unittest`.

---

### Task 1: Create failing tests for threshold market parsing

**Objective:** Define the expected parsing behavior for BTC/ETH “above price by date” markets.

**Files:**
- Create: `tests/test_crypto_market_parsing.py`
- Create: `crypto/__init__.py`
- Create: `crypto/models.py`
- Create: `crypto/data_sources/polymarket.py`

**Step 1: Write failing tests**
- Test parsing of `Will Bitcoin be above $100,000 on June 30?`
- Test parsing of `Will ETH be above $3,500 on May 1?`
- Test rejection of non-threshold questions.

**Step 2: Run test to verify failure**
Run: `python3 -m unittest tests.test_crypto_market_parsing -v`
Expected: FAIL — module/functions missing.

**Step 3: Write minimal implementation**
- Add dataclasses for parsed crypto markets.
- Add parser for BTC/ETH above-threshold questions.

**Step 4: Run tests to verify pass**
Run: `python3 -m unittest tests.test_crypto_market_parsing -v`
Expected: PASS.

---

### Task 2: Create failing tests for probability and scoring model

**Objective:** Lock down the first paper-trading model for crypto threshold markets.

**Files:**
- Create: `tests/test_crypto_threshold_strategy.py`
- Create: `crypto/strategies/__init__.py`
- Create: `crypto/strategies/base.py`
- Create: `crypto/strategies/crypto_threshold.py`

**Step 1: Write failing tests**
- Probability rises as spot increases.
- Probability falls as strike increases.
- Positive-EV signal only appears when fair probability materially exceeds market ask.
- Kelly sizing is capped and zero when EV <= 0.

**Step 2: Run test to verify failure**
Run: `python3 -m unittest tests.test_crypto_threshold_strategy -v`
Expected: FAIL — module/functions missing.

**Step 3: Write minimal implementation**
- Add probability estimator.
- Add scoring method and signal dataclass.

**Step 4: Run tests to verify pass**
Run: `python3 -m unittest tests.test_crypto_threshold_strategy -v`
Expected: PASS.

---

### Task 3: Add non-disruptive public-data clients

**Objective:** Create standalone clients for Polymarket market discovery and crypto spot prices.

**Files:**
- Modify: `crypto/data_sources/polymarket.py`
- Create: `crypto/data_sources/__init__.py`
- Create: `crypto/data_sources/spot.py`

**Step 1: Write failing tests**
- Parse Gamma event search responses into candidate markets.
- Normalize outcome prices and symbols.

**Step 2: Run tests to verify failure**
Run: `python3 -m unittest tests.test_crypto_market_parsing -v`
Expected: FAIL on response normalization helpers.

**Step 3: Write minimal implementation**
- Add Gamma search client.
- Add Coinbase spot client.

**Step 4: Run tests to verify pass**
Run: `python3 -m unittest tests.test_crypto_market_parsing tests.test_crypto_threshold_strategy -v`
Expected: PASS.

---

### Task 4: Add a separate crypto CLI entrypoint

**Objective:** Provide a new command path for paper scanning crypto markets without modifying `bot_v2.py`.

**Files:**
- Create: `crypto_bot.py`
- Create: `crypto/config.py`
- Create: `crypto/default_config.json`

**Step 1: Write failing tests**
- CLI config loads defaults.
- Scanner returns printable signals from injected fake clients.

**Step 2: Run test to verify failure**
Run: `python3 -m unittest discover -s tests -p 'test_crypto*.py' -v`
Expected: FAIL.

**Step 3: Write minimal implementation**
- Add `scan` command.
- Load separate crypto config.
- Print paper-only BUY/SKIP style signals.

**Step 4: Run tests to verify pass**
Run: `python3 -m unittest discover -s tests -p 'test_crypto*.py' -v`
Expected: PASS.

---

### Task 5: Verify non-disruption

**Objective:** Prove the weather bot path is untouched.

**Files:**
- No production file changes required.

**Step 1: Verify diff scope**
Run: `git diff --stat`
Expected: new crypto files only, no behavior changes to `bot_v2.py`.

**Step 2: Verify weather bot status still runs**
Run: `cd /home/technosheen/weatherbot && source venv/bin/activate && python3 bot_v2.py status`
Expected: same status output shape as before.

**Step 3: Verify crypto scan works**
Run: `cd /home/technosheen/weatherbot && source venv/bin/activate && python3 crypto_bot.py scan`
Expected: paper-only crypto signal output or a clean “no candidates found”.
