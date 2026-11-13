import { render, screen } from "@testing-library/react";

import {
  NumericRow as RedesignedNumericRow,
  ReviewStep as RedesignedReviewStep,
} from "@/components/integrations/NightscoutOnboarding/NightscoutOnboarding";
import {
  NumericRow as LegacyNumericRow,
  ReviewStep as LegacyReviewStep,
} from "@/components/integrations/nightscout-onboarding-wizard";
import type { OnboardingDerivation } from "@/lib/api";

const REASON = "Nightscout DIA 10 h is outside the 2-8 h range";
const baseProps = {
  label: "DIA",
  unitsHint: "hours",
  checked: false,
  onToggle: jest.fn(),
  current: 4,
  proposed: 10,
  reason: REASON,
  override: "",
  onOverrideChange: jest.fn(),
  field: "dia_hours",
};

describe.each([
  ["legacy", LegacyNumericRow],
  ["redesigned", RedesignedNumericRow],
])("%s Nightscout onboarding", (_name, NumericRow) => {
  it("shows why an out-of-range DIA is not selected", () => {
    render(<NumericRow {...baseProps} />);

    expect(screen.getByTestId("wizard-reason-dia_hours")).toHaveTextContent(
      REASON,
    );
    expect(screen.getByTestId("wizard-import-dia_hours")).not.toBeChecked();
    expect(screen.getByTestId("wizard-import-dia_hours")).toBeDisabled();
    expect(screen.getByTestId("wizard-override-dia_hours")).toBeEnabled();
    expect(screen.getByTestId("wizard-override-dia_hours")).toHaveAttribute(
      "placeholder",
      "Enter 2–8",
    );
  });

  it("enables DIA import only after a replacement is entered", () => {
    const { rerender } = render(<NumericRow {...baseProps} />);

    rerender(<NumericRow {...baseProps} override="6" />);

    expect(screen.getByTestId("wizard-import-dia_hours")).toBeEnabled();
  });

  it("rejects a DIA override outside 2-8 hours", () => {
    render(<NumericRow {...baseProps} checked override="10" />);

    expect(
      screen.getByText("DIA must be between 2 and 8 hours."),
    ).toBeInTheDocument();
  });
});

const derivation: OnboardingDerivation = {
  has_profile: true,
  units_converted: false,
  units_unknown: false,
  target_low: {
    field: "target_low",
    current_value: 70,
    proposed_value: null,
    default_checked: false,
    reason: null,
  },
  target_high: {
    field: "target_high",
    current_value: 180,
    proposed_value: null,
    default_checked: false,
    reason: null,
  },
  dia_hours: {
    field: "dia_hours",
    current_value: 4,
    proposed_value: 10,
    default_checked: false,
    reason: REASON,
  },
  basal_schedule: {
    field: "basal_schedule",
    current_segments: null,
    proposed_segments: null,
    default_checked: false,
  },
  carb_ratio_schedule: {
    field: "carb_ratio_schedule",
    current_segments: null,
    proposed_segments: null,
    default_checked: false,
  },
  isf_schedule: {
    field: "isf_schedule",
    current_segments: null,
    proposed_segments: null,
    default_checked: false,
  },
};

describe.each([
  ["legacy", LegacyReviewStep],
  ["redesigned", RedesignedReviewStep],
])("%s Nightscout review step", (_name, ReviewStep) => {
  it("blocks Apply when selected DIA has no replacement", () => {
    render(
      <ReviewStep
        derivation={derivation}
        discovery={null}
        imports={{
          target_low: false,
          target_high: false,
          dia_hours: true,
          basal_schedule: false,
          carb_ratio_schedule: false,
          isf_schedule: false,
        }}
        overrides={{ target_low: "", target_high: "", dia_hours: "" }}
        initialSyncWindowDays={7}
        confirmUnitsUnknown={false}
        applyError={null}
        onToggleImport={jest.fn()}
        onUpdateOverride={jest.fn()}
        onSyncWindowChange={jest.fn()}
        onConfirmUnitsToggle={jest.fn()}
        onApply={jest.fn()}
        isApplying={false}
      />,
    );

    expect(screen.getByTestId("wizard-apply")).toBeDisabled();
    expect(
      screen.getByText(
        "Enter a DIA override between 2 and 8 hours, or uncheck DIA.",
      ),
    ).toBeInTheDocument();
  });
});
