/*
 * test_parse: the MQTT payload parsing of batmonitor.c (bm_parse.c) with real payloads, incl. the broken ones a
 * publisher can send: a missing key, null, a string, an out-of-range or infinite number never becomes 0 W.
 */
#include "bm_parse.h"
#include <stdio.h>
#include <string.h>

static int checks, fails;
#define CHECK(cond, ...) do { checks++; if (!(cond)) { fails++; printf("  FAIL %s:%d: ", __func__, __LINE__); \
	printf(__VA_ARGS__); putchar('\n'); } } while (0)

#define FULL "\"ActivePower_PCC_Total\":-1.25,\"ActivePower_PCC_Total_avg5\":-1.5,\"Power_Bat1\":0.4,"     \
			 "\"Power_Bat1_avg5\":0.3,\"Power_PV1\":2.0,\"Power_PV2\":1.5,\"SOC_Bat1\":37"

static void test_inverter(void)
{
	struct bm_inv_sample s;
	memset(&s, 0, sizeof(s));
	CHECK(bm_parse_inverter("{" FULL "}", &s), "full message accepted");
	CHECK(s.pcc == -1250 && s.pcc_avg5 == -1500 && s.bat1 == 400 && s.bat1_avg5 == 300, "kW -> W");
	CHECK(s.have_pv && s.pv == 3500 && s.have_soc_bat1 && s.soc_bat1 == 37, "PV and SOC_Bat1");

	/* the reported case: a message without the PCC must not be a valid PCC of 0 W */
	CHECK(!bm_parse_inverter("{\"Power_Bat1\":0.4,\"Power_PV1\":2,\"Power_PV2\":1}", &s), "PCC missing: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":null,\"Power_Bat1\":0.4}", &s), "PCC null: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":\"-1.2\",\"Power_Bat1\":0.4}", &s), "PCC string: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":1e999,\"Power_Bat1\":0.4}", &s), "PCC infinite: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":250,\"Power_Bat1\":0.4}", &s), "PCC 250 kW: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":-1.2}", &s), "Power_Bat1 missing: rejected");
	CHECK(!bm_parse_inverter("{\"ActivePower_PCC_Total\":-1.2,\"Power_Bat1\":null}", &s), "Power_Bat1 null: rejected");
	CHECK(!bm_parse_inverter("[1,2]", &s) && !bm_parse_inverter("not json", &s) && !bm_parse_inverter("", &s),
		  "no object: rejected");

	memset(&s, 0, sizeof(s));
	CHECK(bm_parse_inverter("{\"ActivePower_PCC_Total\":-1.2,\"Power_Bat1\":0.4,\"Power_PV1\":2}", &s)
		  && !s.have_pv, "PV2 missing: sample usable, but no PV (no night from missing data)");
	CHECK(s.pcc_avg5 == -1200 && s.bat1_avg5 == 400, "5 min means missing: the instant values");
	CHECK(!s.have_soc_bat1 && s.soc_bat1 < 0, "SOC_Bat1 missing: none");
	CHECK(bm_parse_inverter("{\"ActivePower_PCC_Total\":0,\"Power_Bat1\":0,\"SOC_Bat1\":140}", &s)
		  && s.pcc == 0 && !s.have_soc_bat1, "a real 0 kW is valid; SOC 140 %% is none");
}

static void test_r290(void)
{
	int hz = -1;
	CHECK(bm_parse_r290("{\"comp_freq_actual\":48}", &hz) && hz == 48, "R290 48 Hz");
	hz = -1;
	CHECK(!bm_parse_r290("{\"other\":1}", &hz) && hz == -1, "comp_freq_actual missing: rejected, unchanged");
	CHECK(!bm_parse_r290("{\"comp_freq_actual\":null}", &hz), "null: rejected");
}

static void test_number(void)
{
	double v = -1;
	CHECK(bm_parse_number("1234.5", -1000, 30000, &v) && v == 1234.5, "plain number");
	CHECK(bm_parse_number(" 812 \n", -1000, 30000, &v) && v == 812, "whitespace around");
	CHECK(!bm_parse_number("123xyz", -1000, 30000, &v), "trailing text: rejected");
	CHECK(!bm_parse_number("", -1000, 30000, &v) && !bm_parse_number("abc", -1000, 30000, &v), "no number");
	CHECK(!bm_parse_number("nan", -1000, 30000, &v) && !bm_parse_number("inf", -1000, 30000, &v), "nan / inf");
	CHECK(!bm_parse_number("40000", -1000, 30000, &v), "out of range");
}

int main(void)
{
	test_inverter();
	test_r290();
	test_number();
	printf("%d checks, %d failed\n", checks, fails);
	return fails != 0;
}
