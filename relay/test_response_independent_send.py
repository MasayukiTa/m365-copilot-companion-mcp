from relay import copilot_autopilot_relay as relay


class _Keyboard:
    def press(self, key):
        pass

    def insert_text(self, text):
        self.text = text


class _Composer:
    def click(self, **kwargs):
        pass


class _Locator:
    first = _Composer()

    def count(self):
        return 0


class _Page:
    def __init__(self):
        self.keyboard = _Keyboard()

    def locator(self, selector):
        return _Locator()

    def wait_for_timeout(self, ms):
        pass


def test_send_can_skip_all_assistant_response_dom_probes(monkeypatch):
    driver = relay.CopilotWebDriver(_Page())
    monkeypatch.setattr(relay, "_page_network_available", lambda page: True)
    monkeypatch.setattr(driver, "_page_alive", lambda: True)
    monkeypatch.setattr(driver, "_is_generating", lambda: False)
    monkeypatch.setattr(driver, "_wait_send_armed", lambda timeout_s: True)
    monkeypatch.setattr(driver, "_send_button", lambda: None)
    monkeypatch.setattr(driver, "_composer_text", lambda: "")
    monkeypatch.setattr(
        driver, "_answers",
        lambda: (_ for _ in ()).throw(AssertionError("assistant response DOM was queried")),
    )
    driver.send("RUN job_1 seq=1 worker=w", track_answer=False)
    assert driver.answer_content_reads == 0
    assert driver.page.keyboard.text == "RUN job_1 seq=1 worker=w"


def test_answer_read_counter_is_observable(monkeypatch):
    driver = relay.CopilotWebDriver(_Page())
    monkeypatch.setattr(driver, "_answers", lambda: type("A", (), {"count": lambda self: 0})())
    assert driver.read_last_response() == ""
    assert driver.answer_content_reads == 1
