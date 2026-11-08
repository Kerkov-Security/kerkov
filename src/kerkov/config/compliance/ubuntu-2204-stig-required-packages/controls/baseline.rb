{
  'UBTU-22-215010' => ['libpam-pwquality', 0.5],
  'UBTU-22-215015' => ['chrony', 0.3],
  'UBTU-22-653010' => ['auditd', 0.5]
}.each do |id, (name, severity)|
  control id do
    impact severity
    title "Required package #{name} is installed"
    desc 'Package presence only; service operation and configuration require separate assessment.'
    tag stig_id: id
    ref 'Ubuntu 22.04 STIG V2R7 mapping', url: 'https://github.com/ComplianceAsCode/content/blob/bb706dd4dddcfc76df85712874a7e0544821cb97/controls/stig_ubuntu2204.yml'
    describe package(name) do
      it { should be_installed }
    end
  end
end
