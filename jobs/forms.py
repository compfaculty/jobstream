from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Application, Document


class RegistrationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username",)


class ApplicationForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ("recipient", "subject", "body", "document")
        widgets = {"body": forms.Textarea(attrs={"rows": 10})}

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].required = self.instance.channel == "email"
        self.fields["document"].queryset = Document.objects.filter(owner=owner)
        self.fields["document"].label_from_instance = lambda obj: obj.name
        from .services.secret_telaviv import supported

        self.secret_portal = (
            self.instance.channel == "portal" and supported(self.instance)
            if self.instance.job_id
            else False
        )
        if self.secret_portal:
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to Secret Tel Aviv)"
            for name, field in SecretApplicantForm.base_fields.items():
                import copy

                self.fields[f"secret_{name}"] = copy.deepcopy(field)
                self.initial[f"secret_{name}"] = self.instance.portal_answers.get(
                    name, False if name in {"agree_terms", "forward_headhunters"} else ""
                )
        from .services.cato_greenhouse import supported as cato_supported

        self.cato_portal = bool(self.instance.job_id and cato_supported(self.instance))
        if self.cato_portal:
            import copy

            from .services.cato_greenhouse import FIELDS
            from .services.saved_answers import read_answers

            defaults = {}
            if not self.instance.pk and not self.is_bound:
                try:
                    saved, reuse = read_answers(owner.pk)
                    defaults = {
                        key: value for key, value in saved.items() if reuse and key in FIELDS
                    }
                except forms.ValidationError:
                    pass  # Saving a draft is still possible with manually entered answers.
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to Cato)"
            for name in CatoApplicantForm.base_fields:
                self.fields[f"cato_{name}"] = copy.deepcopy(CatoApplicantForm.base_fields[name])
                self.fields[f"cato_{name}"].required = name != "linkedin"
                self.initial[f"cato_{name}"] = self.instance.portal_answers.get(
                    name, defaults.get(name, "")
                )

    def clean(self):
        values = super().clean()
        if any(c in values.get("recipient", "") + values.get("subject", "") for c in "\r\n"):
            raise forms.ValidationError("Headers cannot contain newlines.")
        if self.secret_portal:
            values["body"] = values.get("body") or "Secret Tel Aviv application."
            from .services.secret_telaviv import TERMS_URL, validate_answers

            answers = {
                name: values.get(f"secret_{name}") for name in SecretApplicantForm.base_fields
            }
            answers["terms_url"] = TERMS_URL
            if not self.errors:
                validate_answers(answers)
                self.instance.portal_answers = values["portal_answers"] = answers
        if self.cato_portal:
            from .services.cato_greenhouse import validate_answers

            values["body"] = values.get("body") or "Cato Networks application."
            answers = {
                name: values.get(f"cato_{name}", "") for name in CatoApplicantForm.base_fields
            }
            if not self.errors:
                validate_answers(answers)
                self.instance.portal_answers = values["portal_answers"] = answers
        return values


class TrackingForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ("tracking_status", "notes")


class UploadForm(forms.Form):
    file = forms.FileField(label="Resume (PDF, maximum 5 MiB)")


class ResumeProfileForm(forms.Form):
    name = forms.CharField(max_length=150, label="Full name")
    contact = forms.CharField(max_length=500, required=False, label="Contact details")
    summary = forms.CharField(widget=forms.Textarea, required=False)
    skills = forms.CharField(widget=forms.Textarea, required=False)
    experience = forms.CharField(widget=forms.Textarea, required=False)
    projects = forms.CharField(widget=forms.Textarea, required=False)
    education = forms.CharField(widget=forms.Textarea, required=False)
    other = forms.CharField(widget=forms.Textarea, required=False)
    confirmed = forms.BooleanField(
        label="I confirm these facts, including any additions, are accurate."
    )


class SecretApplicantForm(forms.Form):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Family name")
    email = forms.EmailField(label="Applicant email")
    phone = forms.CharField(max_length=100, label="Telephone")
    agree_terms = forms.BooleanField(label="I accept the Secret Tel Aviv terms and conditions")
    forward_headhunters = forms.BooleanField(required=False, label="Forward my CV to headhunters")

    def answers(self):
        from .services.secret_telaviv import TERMS_URL

        return {**self.cleaned_data, "terms_url": TERMS_URL}


class ApplicationPreferencesForm(forms.Form):
    preferred_locations = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Preferred locations",
        help_text="One country, city or region per line.",
    )
    location_required = forms.BooleanField(required=False, label="Only apply in these locations")
    preferred_titles = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Preferred job titles",
        help_text="One title or title phrase per line.",
    )
    title_required = forms.BooleanField(required=False, label="Only apply to these titles")
    wanted_technologies = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Technologies I want to work with",
    )
    technology_required = forms.BooleanField(
        required=False, label="Require at least one wanted technology in the job"
    )
    excluded_technologies = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Do not apply when these technologies are mandatory",
    )
    remote_only = forms.BooleanField(required=False, label="Only apply to explicitly remote jobs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services.normalization import TECHNOLOGIES

        choices = [(name, name) for name in TECHNOLOGIES]
        for field in ("excluded_technologies", "wanted_technologies"):
            self.fields[field].choices = choices

    def clean(self):
        data = super().clean()
        for field in ("preferred_locations", "preferred_titles"):
            values = list(
                dict.fromkeys(
                    line.strip() for line in data.get(field, "").splitlines() if line.strip()
                )
            )
            if len(values) > 30 or any(len(value) > 150 for value in values):
                self.add_error(field, "Use at most 30 lines, up to 150 characters per line.")
            data[field] = values
        for field in ("excluded_technologies", "wanted_technologies"):
            data[field] = list(dict.fromkeys(data.get(field, [])))
        if set(data["wanted_technologies"]) & set(data["excluded_technologies"]):
            self.add_error(
                "wanted_technologies", "A technology cannot be both wanted and excluded."
            )
        for flag, field in (
            ("location_required", "preferred_locations"),
            ("title_required", "preferred_titles"),
            ("technology_required", "wanted_technologies"),
        ):
            if data.get(flag) and not data.get(field):
                self.add_error(field, "Add at least one value before enabling this limit.")
        return data


class CommonAnswersForm(forms.Form):
    first_name = forms.CharField(required=False, max_length=150, label="First name")
    last_name = forms.CharField(required=False, max_length=150, label="Family name")
    email = forms.EmailField(required=False, label="Application email")
    phone = forms.RegexField(
        r"^\+[1-9]\d{6,14}$",
        required=False,
        max_length=16,
        label="Phone with country code",
        help_text="International format, for example +972501234567.",
    )
    phone_country = forms.RegexField(
        r"^[A-Za-z]{2}$",
        required=False,
        max_length=2,
        label="Phone country code",
        help_text="Two-letter country code, for example IL, US or GB.",
    )
    city = forms.CharField(required=False, max_length=150, label="Current city")
    country = forms.CharField(required=False, max_length=150, label="Current country")
    linkedin = forms.URLField(
        required=False, max_length=500, label="LinkedIn profile", assume_scheme="https"
    )
    notice_period = forms.CharField(required=False, max_length=150, label="Notice period")
    work_authorization_notes = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Work authorization notes",
        help_text="Country-specific facts for review; never automatically "
        "converted into a yes/no answer.",
    )
    gender_identity = forms.ChoiceField(
        required=False,
        label="Gender identity (optional)",
        choices=[
            ("", "Leave unanswered / ask when needed"),
            ("male", "Male"),
            ("female", "Female"),
            ("nonbinary", "Non-binary"),
            ("prefer_not_to_answer", "Prefer not to answer"),
        ],
        help_text="Only for an equivalent voluntary gender-identity question, "
        "not legal-sex questions.",
    )
    reuse_enabled = forms.BooleanField(
        required=False,
        label="Allow these saved answers to prefill compatible application questions",
    )

    def clean_phone_country(self):
        import phonenumbers

        value = self.cleaned_data["phone_country"].upper()
        if value and value not in phonenumbers.SUPPORTED_REGIONS:
            raise forms.ValidationError("Enter a supported two-letter country code.")
        return value


class CatoApplicantForm(CommonAnswersForm):
    """Only contact questions with established meaning; no demographic inference."""

    notice_period = None
    work_authorization_notes = None
    gender_identity = None
    reuse_enabled = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name != "linkedin"
