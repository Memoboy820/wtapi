VEHICLE_NAMES: dict[str, str] = {
    # США
    "f_14a_early": "F-14A Early",
    "f_14a_iriaf": "F-14A IRIAF",
    "f_14b": "F-14B",
    "f_14d": "F-14D",

    "f_15a": "F-15A",
    "f_15c_msip2": "F-15C MSIP II",
    "f_15c_golden_eagle": "F-15C Golden Eagle",
    "f_15e": "F-15E",
    "f_15ex": "F-15EX",

    "f_16a_block_10": "F-16A Block 10",
    "f_16a_block_15_adf": "F-16A ADF",
    "f_16c_block_50": "F-16C Block 50",
    "f_16xl": "F-16XL",
    "f_20a": "F-20A",

    "fa_18a": "F/A-18A",
    "fa_18c_early": "F/A-18C Early",
    "fa_18c_late": "F/A-18C Late",
    "fa_18d_late": "F/A-18D Late",
    "fa_18e_block_2": "F/A-18E Block II",

    "a_10a_early": "A-10A Early",
    "a_10a_late": "A-10A Late",
    "a_10c": "A-10C",

    # СССР / Россия
    "su_27": "Су-27",
    "su_27sm": "Су-27СМ",
    "su_30sm": "Су-30СМ",
    "su_30sm2": "Су-30СМ2",
    "su_33": "Су-33",
    "su_34": "Су-34",

    "su_25": "Су-25",
    "su_25k": "Су-25К",
    "su_25t": "Су-25Т",
    "su_25tm": "Су-25ТМ",
    "su_25sm3": "Су-25СМ3",

    "mig_29_9_13": "МиГ-29 (9-13)",
    "mig_29smt_9_19": "МиГ-29СМТ (9-19)",
    "mig_29m_9_15": "МиГ-29М (9-15)",
    "mig_23m": "МиГ-23М",
    "mig_23ml": "МиГ-23МЛ",
    "mig_23mld": "МиГ-23МЛД",
    "mig_27k": "МиГ-27К",
    "mig_27m": "МиГ-27М",

    "ka_50": "Ка-50",
    "ka_52": "Ка-52",
    "mi_28n": "Ми-28Н",
    "mi_28nm": "Ми-28НМ",

    # Франция
    "rafale_c_f3": "Rafale C F3",
    "rafale_m_f3r": "Rafale M F3-R",
    "mirage_2000_5f": "Mirage 2000-5F",
    "mirage_2000c_s4": "Mirage 2000C-S4",
    "mirage_2000c_s5": "Mirage 2000C-S5",
    "mirage_2000d_r1": "Mirage 2000D-R1",
    "mirage_2000d_rmv": "Mirage 2000D RMV",
    "mirage_4000": "Mirage 4000",

    # Швеция
    "saab_jas39a": "JAS39A",
    "saab_jas39c": "JAS39C",
    "saab_jas39d": "JAS39D",
    "saab_jas39e": "JAS39E",
    "saab_ja37": "JA37",
    "saab_ja37d": "JA37D",

    # Китай
    "j_10a": "J-10A",
    "j_10c": "J-10C",
    "j_11": "J-11",
    "j_11a": "J-11A",
    "j_11b": "J-11B",
    "j_16": "J-16",
    "jf_17": "JF-17",

    # Германия
    "germ_leopard_2a4": "Leopard 2A4",
    "germ_leopard_2a5": "Leopard 2A5",
    "germ_leopard_2a6": "Leopard 2A6",
    "germ_leopard_2a7v": "Leopard 2A7V",
    "germ_leopard_2a8": "Leopard 2A8",
    "germ_leopard_2pl": "Leopard 2PL",
    "germ_leopard_2k": "Leopard 2K",
    "germ_pzkpfw_maus": "Maus",
}


def get_vehicle_name(vehicle_id: str) -> str:
    key = vehicle_id.removeprefix("tankModels/")
    return VEHICLE_NAMES.get(key, key)