"""Versioned scorekeeping policies; this app does not play cards for the user."""


def definition(slug, name, direction, minimum, maximum, note, *, negative=False,
               fixed=None, modes=None, dealer=False, targets=None, target=None):
    return dict(slug=slug, name=name, description=note,
                scoring_direction=direction, min_players=minimum, max_players=maximum,
                fixed_rounds=fixed, round_labels={str(n): {11: 'J', 12: 'Q', 13: 'K'}.get(n, str(n)) for n in fixed or []},
                modes=modes or ['rounds'], allow_negative=negative, has_dealer=dealer,
                targets=targets or [], default_target=target, rule_note=note,
                image=f'/art/{slug}.png')


CATALOG = {
    g['slug']: g for g in [
        definition('five-crowns', 'Five Crowns', 'low', 2, 7,
                   'Eleven hands, 3 through King. Lowest total wins; ties are shared.', fixed=list(range(3, 14)), dealer=True),
        definition('uno-classic', 'UNO Classic', 'high', 2, 10,
                   'Enter awarded hand totals. Choose standard high-total scoring or the published low-total alternative. Targets prompt only.',
                   dealer=True, targets=[100, 200, 300, 500], target=500),
        definition('uno-flip', 'UNO Flip', 'high', 2, 10,
                   'Enter awarded hand totals and retain each hand’s light or dark side. Standard high-total or published low-total alternative.',
                   dealer=True, targets=[100, 200, 300, 500], target=500),
        definition('dutch-blitz', 'Dutch Blitz', 'high', 2, 8,
                   'Enter signed hand totals. High total is standard; low total is a house-rule alternative.',
                   negative=True, targets=[75, 100, 150], target=75),
        definition('skyjo', 'Skyjo', 'low', 2, 8,
                   'Enter already adjusted signed round totals. The app never doubles them again. Lowest total wins.',
                   negative=True, targets=[100], target=100),
        definition('gin-rummy', 'Gin Rummy', 'high', 2, 8,
                   'Enter nonnegative awarded hand totals, including your agreed bonuses. No card or bonus engine.',
                   targets=[100, 200, 300, 500], target=100),
        definition('sevens', 'Sevens', 'low', 2, 7,
                   'Enter nonnegative penalties. Any player reaching the target prompts End or Continue; lowest total wins.',
                   targets=[100], target=100),
        definition('basic-other', 'Basic / Other', 'high', 1, 10,
                   'Choose signed scored rounds with high or low total, or select a winner without inventing points.',
                   negative=True, modes=['rounds', 'winner']),
        definition('kings-corner', 'Kings in the Corner', 'low', 2, 4,
                   'Multi-round mode totals penalties, lowest wins. Single mode awards the winner the sum of opponent penalties.',
                   modes=['rounds', 'single']),
        definition('trouble', 'Trouble', 'high', 2, 4,
                   'Record four pegs per player. Winner strength is 25 + 5 per opponent board peg + 10 per opponent home peg.',
                   modes=['single']),
    ]
}

CATALOG['uno-classic']['rule_source'] = 'https://service.mattel.com/instruction_sheets/W2085.pdf'
CATALOG['uno-flip']['rule_source'] = 'https://service.mattel.com/instruction_sheets/GDR44-English.pdf'
