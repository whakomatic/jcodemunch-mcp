// Representative UnrealScript sample covering every construct that the
// regex extractor in _parse_unrealscript_symbols() is expected to handle.
class Weapon extends Actor
    within Pawn
    config(Game)
    abstract
    native;

const MAX_AMMO = 30;
const DEFAULT_NAME = "Pistol";
var config int Health;           // pulls initial value from [Weapon] section of XcomGame.ini
var(Weapon) float Accuracy;      // category + modifier
var config transient int AmmoCount, ClipSize;  // multiname
var array<FireInfo> FireHistory;  // generic type

enum EFireMode
{
    FM_Single,
    FM_Burst,
    FM_Auto
};

struct native immutable FireInfo
{
    var int   Damage;
    var float Range;
    var name  HitSound;
};

/** Return the damage for this weapon given its base damage. */
simulated function int GetDamage(int BaseDamage, optional float Multiplier)
{
    local int Result;
    Result = BaseDamage * Multiplier;
    return Clamp(Result, 0, MAX_AMMO);
}

native(1024) static final function Weapon FindClosest(Actor Source);

event PostBeginPlay()
{
    super.PostBeginPlay();
}

auto state Idle
{
    function Tick(float DeltaTime)
    {
        // Idle tick body.
    }

Begin:
    Sleep(0.5);
}

state Firing extends Idle
{
    simulated event BeginState(name PreviousStateName)
    {
        super.BeginState(PreviousStateName);
    }
}

defaultproperties
{
    Health=100
    FireMode=FM_Auto
    Inventory=(WeaponClass=class'Game.Pistol',Ammo=30)
    Message="If the regex sees class=Trap inside defaults, it is broken."
}
