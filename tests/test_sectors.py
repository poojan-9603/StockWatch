from quant.sectors import division, gics11


def test_gics11_handles_the_exceptions_inside_major_groups():
    assert gics11("2834") == "Health Care"            # pharma inside chemicals
    assert gics11("2821") == "Materials"              # plastics, the rest of chemicals
    assert gics11("3674") == "Information Technology" # semiconductors inside electronic equipment
    assert gics11("3711") == "Consumer Discretionary" # cars inside transportation equipment
    assert gics11("7370") == "Communication Services" and gics11("7372") == "Information Technology"
    assert gics11("6798") == "Real Estate" and gics11("6770") == "Financials"
    assert gics11("") is None and division("2834") == "D Manufacturing"
