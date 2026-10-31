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
